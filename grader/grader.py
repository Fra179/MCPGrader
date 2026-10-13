from collections import defaultdict
import json
from typing import Any, Iterable
from config import ProgramConfig, AssignmentConfig, AssignmentTaskConfig
from git import Repo, Git, exc
from aggregation import AGG_NAME_TO_CLASS
from pathlib import Path
from os import mkdir
from shutil import rmtree
from logging import Logger
from runners import *
from shutil import copyfile
import subprocess
import signal
from .structs import GradeResult, TaskGradeResult
from logger import build_logger
import os
from pusher.github_pusher import GithubPusher
from grader_worker import GraderWorkerAPI, SubmissionInfo
import shlex
import tempfile
from contextlib import contextmanager

KILL_GRACE_SECONDS = 5  # how long a timed-out test script gets after each kill signal

@contextmanager
def deploy_key_env(deploy_key: str):
    """Write the submitted private key to a 0600 temp file and yield the env that makes git use it."""
    key = deploy_key.replace("\r\n", "\n").strip() + "\n"  # ssh rejects keys without a trailing newline

    fd, path = tempfile.mkstemp(prefix="deploy_key_")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(key)
        yield {
            "GIT_SSH_COMMAND": f"ssh -F /dev/null -i {shlex.quote(path)} -o IdentitiesOnly=yes -o BatchMode=yes -o StrictHostKeyChecking=accept-new"
        }
    finally:
        os.unlink(path)

class Grader:
    def __init__(self, config: ProgramConfig, logger: Logger) -> None:
        self.config = config
        self.wd = Path(config.grader.working_dir)
        self.log = logger
        self.job_ids: list[tuple] = []
        self.runner: ABRunner = self._get_runner()
        self.__cache = {}

        if not (self.wd / ".cache").exists():
            mkdir(self.wd / ".cache")

    def _open_caches_for_assignment(self, assignment_cfg: AssignmentConfig) -> None:
        for task in assignment_cfg.tasks:
            self.__cache[task] = self._open_cache_file(task, assignment_cfg)

    def _open_cache_file(self, task: AssignmentTaskConfig, assignment: AssignmentConfig) -> dict[str, Any]:
        cache_file_path = self.wd / ".cache" / f"{assignment.name}_{task.name}_cache.json"
        if not cache_file_path.exists():
            with open(cache_file_path, 'w') as cache_file:
                json.dump({"perf_hash": task.performance_hash(), "cache": {}}, cache_file)
        
        with open(cache_file_path, 'r') as cache_file:
            cache: dict[str, Any] = json.load(cache_file)

        if cache.get("perf_hash") != task.performance_hash():
            cache["perf_hash"] = task.performance_hash()
            cache["cache"] = {} 
        
        return cache
    
    def _save_cache_file(self, task: AssignmentTaskConfig, assignment: AssignmentConfig, cache: dict[str, Any]) -> None:
        cache_file_path = self.wd / ".cache" / f"{assignment.name}_{task.name}_cache.json"
        with open(cache_file_path, 'w') as cache_file:
            json.dump(cache, cache_file, indent=4)

    def _save_cache_file_for_assignment(self, assignment: AssignmentConfig) -> None:
        for task in assignment.tasks:
            cache = self.__cache.get(task)
            if cache is not None:
                self._save_cache_file(task, assignment, cache)
                self.log.debug("Saved cache for task %s[%s]", assignment.name, task.name)

    def _grade_assignment(self, assignment_cfg: AssignmentConfig) -> None:
        worker = GraderWorkerAPI(self.config.grader.worker_url, self.config.grader.worker_private_key)
        submissions = worker.get_submissions(assignment_cfg.name)
        self.log.info("Found %d submissions for assignment %s", len(submissions), assignment_cfg.name)

        commit_hashes = self._get_latest_commit_hashes(submissions) if any(not task.skip for task in assignment_cfg.tasks) else {}

        for task in assignment_cfg.tasks:
            blocking = task.blocking

            if task.skip:
                self.log.info("Skipping grading for task: %s[%s]", assignment_cfg.name, task.name)
                self.job_ids.append((assignment_cfg, task, None))
                continue

            updated_submissions = self._filter_updated_submissions(task, submissions, commit_hashes)
            if not updated_submissions:
                self.log.info("No updated submissions for task %s[%s], not launching a job", assignment_cfg.name, task.name)
                continue

            # Clear results left by a previous run, so only this run's job is read back
            results_dir = self._results_dir(assignment_cfg, task)
            rmtree(results_dir, ignore_errors=True)
            results_dir.mkdir(parents=True)

            self.log.info("Launching grading job for task %s[%s] with %d updated submissions", assignment_cfg.name, task.name, len(updated_submissions))
            # TODO: submitit pickles the arguments (including every student's deploy_key) into
            # <working_dir>/slurm_logs/*_submitted.pkl and never deletes them, so the keys stay on disk.
            job_id = self.runner.run(self._grade_task, task, updated_submissions, results_dir)
            self.job_ids.append((assignment_cfg, task, job_id))

            if blocking:
                self.log.info("Waiting for blocking task %s[%s] to complete", assignment_cfg.name, task.name)
                self.runner.wait(job_id)

    def _repo_full_name(self, submission: SubmissionInfo) -> str:
        return submission.ssh_url.split(":", 1)[1].removesuffix(".git")

    def _get_latest_commit_hash(self, submission: SubmissionInfo) -> str:
        with deploy_key_env(submission.deploy_key) as env:
            git = Git()  # fresh instance so the per-submission env isn't shared
            with git.custom_environment(**env):
                return git.ls_remote(submission.ssh_url, "HEAD").split()[0]

    def _get_latest_commit_hashes(self, submissions: Iterable[SubmissionInfo]) -> dict[str, str | None]:
        """HEAD commit of each non-empty submission, keyed by ssh_url; None if it couldn't be fetched."""
        commit_hashes: dict[str, str | None] = {}

        for submission in submissions:
            try:
                commit_hashes[submission.ssh_url] = self._get_latest_commit_hash(submission)
            except exc.GitCommandError:
                self.log.warning("Failed to get commit hash for submission %s, marking as updated", submission.repo_url)
                commit_hashes[submission.ssh_url] = None
            except IndexError:
                self.log.info("Submission %s has no commits, skipping grading", submission.repo_url)

        return commit_hashes

    def _filter_updated_submissions(self, task: AssignmentTaskConfig, submissions: Iterable[SubmissionInfo], commit_hashes: dict[str, str | None]) -> list[SubmissionInfo]:
        cache = self.__cache[task]["cache"]
        updated_submissions = []

        for submission in submissions:
            if submission.ssh_url not in commit_hashes:  # empty repository
                continue

            commit_hash = commit_hashes[submission.ssh_url]
            if commit_hash is None or cache.get(submission.ssh_url) != commit_hash:
                updated_submissions.append(submission)

        return updated_submissions

    def _results_dir(self, assignment: AssignmentConfig, task: AssignmentTaskConfig) -> Path:
        return self.wd / "results" / assignment.name / task.name

    def _grade_task(self, task: AssignmentTaskConfig, submissions: Iterable[SubmissionInfo], results_dir: Path) -> None:
        # Each result is written to results_dir as soon as it's ready, so if the job dies
        # only the submission being graded at that moment is lost
        task_id = int(os.environ.get('SLURM_PROCID', 0))

        if task_id != 0:
            return

        logger = build_logger(name=f"grader.task.{task.name}", level=self.log.level)

        for submission in submissions:
            try:
                res = self._grade_submission(submission, task, logger)
                self._write_result(results_dir, res)
            except Exception as e:
                logger.error("Failed to grade submission %s[%s]: %s", submission.repo_url, task.name, str(e))

    def _write_result(self, results_dir: Path, result: TaskGradeResult) -> None:
        result_path = results_dir / f"{result['name']}.json"
        tmp_path = result_path.with_suffix(".json.tmp")
        with open(tmp_path, 'w') as f:
            json.dump(result, f)
        os.replace(tmp_path, result_path)  # atomic, so a partially written result is never read

    def _read_results(self, results_dir: Path) -> list[TaskGradeResult]:
        results = []
        for result_path in sorted(results_dir.glob("*.json")):
            try:
                with open(result_path, 'r') as f:
                    results.append(json.load(f))
            except (OSError, json.JSONDecodeError) as e:
                self.log.error("Failed to read result file %s: %s", result_path, str(e))
        return results
           
    def _grade_submission(self, submission: SubmissionInfo, task: AssignmentTaskConfig, log: Logger) -> TaskGradeResult:
        full_name = self._repo_full_name(submission)
        log.info("Grading submission for %s[%s]", full_name, task.name)
        repo_dir = self.wd / (full_name.replace('/', '_') + f"_{task.name}")
        
        if repo_dir.exists():
            log.info("Cleaning up existing repository directory %s", repo_dir)
            rmtree(repo_dir)

        log.debug("Downloading %s", submission.repo_url)
        with deploy_key_env(submission.deploy_key) as env:
            repo = Repo.clone_from(submission.ssh_url, repo_dir, env=env)

        commit_hash = repo.head.commit.hexsha
        log.debug("Cloned repository at commit %s", commit_hash)

        result = self._grade_task_submission(task, submission, commit_hash, repo_dir, log)
            
        return result
    
    def _grade_task_submission(self, task: AssignmentTaskConfig, submission: SubmissionInfo, commit_hash: str, repo_dir: Path, log: Logger) -> TaskGradeResult:
        # Use slurm to run the grading script
        # Copy the grading script to the repo directory

        grading_script_dest = repo_dir / Path(task.test_script_path).name
        copyfile(task.test_script_path, grading_script_dest)

        # Make the grading script executable
        grading_script_dest.chmod(0o755)

        log.debug("Copied grading script to %s", grading_script_dest)

        # Run inside the slurm environment, pipe stdout to a variable
        log.info("Running grading script")
        
        data = {}
        error = ""
        status = ""
        stdout = ""
        runtimes = []
        
        returncode, stdout, stderr = self._run_test_script(grading_script_dest, repo_dir, task.submission_timeout, log)

        if returncode is None:
            log.error("Grading script timed out after %ss", task.submission_timeout)
            error = f"Grading script timed out after {task.submission_timeout}s\n" + stderr
            status = "timeout"
        elif returncode != 0:
            log.error("Grading script failed with error: %s", stderr)
            error = stderr
            status = "error"
        else:
            stdout = stdout.strip()
            last_line = stdout.split('\n')[-1]

            # {'passed': 12, 'total': 12, 'times': [442.44458, 664.421387, 886.576111, 354.137085, 442.864655, 663.164917, 884.586487, 354.348022, 443.62854, 664.1828, 885.255188, 354.565033]}
            try:
                data = json.loads(last_line)
                runtimes = data.get("times", [])
                status = "graded"
            except json.JSONDecodeError:
                log.error("Failed to parse grading script output as JSON: %s", last_line)
                status = "error"
                error = "Failed to parse grading script output as JSON. The script may have crashed or produced invalid output.\nLast line: " + last_line +  "\nFull stderr:\n" + stderr

            log.info("Grading result: %s", data)
            log.info("Grading script output: %s", stdout)

        runtime = sum(runtimes) / len(runtimes) if runtimes else 0.0
        log.info("Average runtime for %s [%s]: %.4f ms", submission.repo_url, task.name, runtime)

        return {
            "name": str(submission.student_id),
            "repo_dir": str(repo_dir), 
            "commit_hash": commit_hash, 
            "status": status, 
            "error": error, 
            "stdout": stdout, 
            "runtimes": runtimes, 
            "data": data, 
            "repo_url": submission.ssh_url,
        }

    def _run_test_script(self, script: Path, cwd: Path, timeout: int | None, log: Logger) -> tuple[int | None, str, str]:
        """Run the test script in its own process group. Returns (returncode, stdout, stderr); returncode is None on timeout."""
        process = subprocess.Popen([script], cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True)
        try:
            stdout, stderr = process.communicate(timeout=timeout)
            return process.returncode, stdout, stderr
        except subprocess.TimeoutExpired:
            pass

        # Kill the whole group, not just the script: make, srun and the student's binary would
        # otherwise keep running and hold the output pipes open. SIGTERM first lets srun clean up.
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(process.pid, sig)
            except ProcessLookupError:
                pass
            try:
                stdout, stderr = process.communicate(timeout=KILL_GRACE_SECONDS)
                return None, stdout, stderr
            except subprocess.TimeoutExpired:
                continue

        # Something that left the process group still holds the pipes: give up on the output
        log.warning("Processes of %s survived SIGKILL of their group, discarding their output", script)
        for pipe in (process.stdout, process.stderr):
            if pipe:
                pipe.close()
        process.wait()
        return None, "", ""

    def _retrieve_results(self, existing_data: dict) -> dict:
        data: dict[tuple[str, str], GradeResult] = defaultdict(lambda: GradeResult("", {}, {}, {}, {}, {}, {}, {}))

        repos_to_cleanup = set()


        # compute the students to keep because they were not re-graded
        # data_to_add: dict[tuple[str, str, str], GradeResult] = defaultdict(lambda: GradeResult("", "", {}, {}, {}, {}, {}))

        for assignment_name, students in existing_data.items():
            for student in students:
                data[(assignment_name, student["name"])] = GradeResult.from_dict(student)

        for assignment_cfg, task, jobid in self.job_ids:
            if jobid is None:
                self.log.info("Skipping result retrieval for skipped task: %s[%s]", assignment_cfg.name, task.name)    
                continue

            self.log.info("Collecting results for %s[%s]", assignment_cfg.name, task.name)
            try:
                self.runner.collect_results(jobid)  # only to surface a job failure
            except Exception as e:
                self.log.error("Grading job for %s[%s] failed, keeping the results it wrote before failing: %s", assignment_cfg.name, task.name, str(e))

            for result in self._read_results(self._results_dir(assignment_cfg, task)):
                name = result["name"]
                data[(assignment_cfg.name, name)].update_from_dict(result, task.name, AGG_NAME_TO_CLASS[task.reduction or "mean"]())
            
                repo_dir = Path(result["repo_dir"])
                if not assignment_cfg.preserve_repo_files and repo_dir.exists():
                    repos_to_cleanup.add(repo_dir)

                # Update the cache with the latest commit hash for this student and task
                self.__cache[task]["cache"][result["repo_url"]] = result["commit_hash"]

        # Cleanup repositories
        for repo_dir in repos_to_cleanup:
            rmtree(repo_dir, ignore_errors=True)
            self.log.debug("Deleted repository files for %s", repo_dir.name.replace('_', '/'))

        result = defaultdict(list)

        for (assignment_name, _), grade_result in data.items():
            result[assignment_name].append(grade_result.to_dict())
        
        return result

    def _get_runner(self) -> ABRunner:
        logs_dir = Path(self.config.grader.working_dir) / "slurm_logs"
        logs_dir.mkdir(parents=True, exist_ok=True)
        return SlurmRunner(logs_folder=str(logs_dir))

    def _load_grades_file(self) -> dict:
        grades_file_path = Path(self.config.grader.grades_file)
        if grades_file_path.exists():
            with open(grades_file_path, 'r') as f:
                return json.load(f)
        return {}
    
    def _save_grades_file(self, data: dict) -> None:
        grades_file_path = Path(self.config.grader.grades_file)
        with open(grades_file_path, 'w') as f:
            json.dump(data, f, indent=4)

    def grade(self):
        data = self._load_grades_file()

        for assignment in self.config.assignments:
            self.log.debug("Loading cache files for assignment: %s", assignment.name)
            self._open_caches_for_assignment(assignment)
            self.log.info("Launching grading job for assignment: %s", assignment.name)
            self._grade_assignment(assignment)

        self.log.info("Waiting for all grading jobs to complete")
        self.runner.wait_all()

        results = self._retrieve_results(data)

        self._save_grades_file(results)

        if self.config.pusher and self.config.pusher.push:
            pusher = GithubPusher(self.config.pusher)
            commit_sha = pusher.push_results(self.config.grader.grades_file)
            self.log.info("Pushed results to GitHub repository %s at commit %s", self.config.pusher.github_repo, commit_sha)

        for assignment in self.config.assignments:
            self._save_cache_file_for_assignment(assignment)