import json
import logging
import os
import signal
import subprocess
import time
from pathlib import Path
from shutil import copyfile, rmtree

from git import Repo, exc

from config import AssignmentTaskConfig
from grader_worker import SubmissionInfo
from logger import build_logger
from .structs import TaskGradeResult
from .utils import deploy_key_env, write_json_atomic

KILL_GRACE_SECONDS = 5  # how long a timed-out test script gets after each kill signal

class SubmissionGrader:
    """Grades the submissions of one task inside its SLURM job: clone, run the test script, write the result file.

    Holds only data, so submitit can pickle `grade_all` into slurm_logs/*_submitted.pkl without
    dragging along `Grader` and its config (GitHub PAT). Keep it that way.
    """

    def __init__(self, task: AssignmentTaskConfig, workdir: Path, preserve_repo_files: bool, log_level: int) -> None:
        self.task = task
        # Computed here, in the main process: the result must carry the configuration the job was
        # submitted with, even if the test script changes before the result is recorded
        self.perf_hash = task.performance_hash()
        # Resolve relative config paths in the coordinator, before jobs change directory.
        self.setup_script = Path(task.setup_script).resolve() if task.setup_script else None
        self.workdir = workdir
        self.preserve_repo_files = preserve_repo_files
        self.log_level = log_level
        self.log = logging.getLogger(f"grader.task.{task.name}")  # handlers are attached in the job

    def grade_all(self, submissions: list[SubmissionInfo], results_dir: Path, commit_hashes: dict[str, str] | None = None, bundle_paths: dict[str, str] | None = None) -> None:
        """Entry point of the SLURM job: grade each submission and write its result file."""
        # Each result is written to results_dir as soon as it's ready, so if the job dies
        # only the submission being graded at that moment is lost
        task_id = int(os.environ.get('SLURM_PROCID', 0))

        if task_id != 0:
            return

        self.log = build_logger(name=self.log.name, level=self.log_level)

        for submission in submissions:
            try:
                commit_hash = commit_hashes[submission.ssh_url] if commit_hashes is not None else None
                bundle = Path(bundle_paths[submission.ssh_url]) if bundle_paths is not None else None
                res = self.grade(submission, commit_hash, bundle)
                write_json_atomic(results_dir / f"{res['name']}.json", res)
            except Exception as e:
                self.log.error("Failed to grade submission %s[%s]: %s", submission.repo_url, self.task.name, str(e))
            finally:
                # Delete each clone right away, so a job holds at most one on disk
                if not self.preserve_repo_files:
                    rmtree(self.repo_dir(submission), ignore_errors=True)

    def repo_dir(self, submission: SubmissionInfo) -> Path:
        # TODO (not urgent): use a more resistant dir name like submission UUID + task
        # replacing '/' to '_' makes course_group/solution and course/group_solution (for example).
        return self.workdir / (self._repo_full_name(submission).replace('/', '_') + f"_{self.task.name}")

    def _repo_full_name(self, submission: SubmissionInfo) -> str:
        return submission.ssh_url.split(":", 1)[1].removesuffix(".git")

    def grade(self, submission: SubmissionInfo, commit_hash: str | None = None, bundle: Path | None = None) -> TaskGradeResult:
        """Clone the submission and grade the requested snapshot commit (HEAD for legacy jobs)."""
        self.log.info("Grading submission for %s[%s]", self._repo_full_name(submission), self.task.name)
        repo_dir = self.repo_dir(submission)
        commit_hash = self._clone_repo(submission, repo_dir, commit_hash, bundle)
        return self._grade_cloned(submission, commit_hash, repo_dir)

    def _clone_repo(self, submission: SubmissionInfo, repo_dir: Path, commit_hash: str | None, bundle: Path | None = None) -> str:
        """Replace the clone, check out the requested commit, and return its actual hash."""
        if repo_dir.exists():
            self.log.info("Cleaning up existing repository directory %s", repo_dir)
            rmtree(repo_dir)

        if bundle is not None:
            if commit_hash is None:
                raise ValueError("A staged repository requires a pinned commit")
            # No network access or deploy key in new jobs. Missing bundles fail locally.
            with Repo.clone_from(str(bundle), repo_dir) as repo:
                repo.git.checkout("--detach", commit_hash)
                return repo.head.commit.hexsha

        # Compatibility with already-pickled jobs and direct callers predating staging.
        self.log.debug("Downloading %s", submission.repo_url)
        with deploy_key_env(submission.deploy_key) as env:
            repo = Repo.clone_from(submission.ssh_url, repo_dir, env=env)
            if commit_hash is not None:
                # The snapshot may no longer be on a branch fetched by clone (e.g. a force-push).
                try:
                    repo.git.cat_file("-e", f"{commit_hash}^{{commit}}")
                except exc.GitCommandError:
                    with repo.git.custom_environment(**env):
                        repo.git.fetch("origin", commit_hash)
                repo.git.checkout("--detach", commit_hash)

        commit_hash = repo.head.commit.hexsha
        self.log.debug("Cloned repository at commit %s", commit_hash)
        return commit_hash

    def _grade_cloned(self, submission: SubmissionInfo, commit_hash: str, repo_dir: Path) -> TaskGradeResult:
        task, log = self.task, self.log

        # Copy the grading script to the repo directory
        grading_script_dest = repo_dir / Path(task.test_script_path).name
        # Remove a student-committed symlink without touching its target.
        grading_script_dest.unlink(missing_ok=True)
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

        # TODO: agree on exit codes with the test scripts (0 graded, 1 student failure,
        # 2 infrastructure failure) and don't cache 2, so it's retried on the next run.
        returncode, stdout, stderr = self._run_test_script(grading_script_dest, repo_dir)

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
            "commit_hash": commit_hash,
            "status": status,
            "error": error,
            "stdout": stdout,
            "runtimes": runtimes,
            "data": data,
            "repo_url": submission.ssh_url,
            "perf_hash": self.perf_hash,
        }

    def _run_test_script(self, script: Path, cwd: Path) -> tuple[int | None, str, str]:
        """Run the test script in its own process group. Returns (returncode, stdout, stderr); returncode is None on timeout."""
        command = [script]
        if setup_script := getattr(self, "setup_script", None):
            # A login shell initializes the cluster's module command. Source and exec in the
            # same shell so Python scripts inherit exports; positional args avoid shell injection.
            command = ["bash", "-l", "-c", 'set -e\nsource "$1"\nexec "$2"', "grader-setup", str(setup_script), str(script)]
        process = subprocess.Popen(command, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True)
        try:
            stdout, stderr = process.communicate(timeout=self.task.submission_timeout)
            return process.returncode, stdout, stderr
        except subprocess.TimeoutExpired:
            pass

        # Kill the whole group, not just the script: make, srun and the student's binary would
        # otherwise keep running and hold the output pipes open. SIGTERM first lets srun clean up.
        for sig in (signal.SIGTERM, signal.SIGKILL):
            deadline = time.monotonic() + KILL_GRACE_SECONDS
            try:
                os.killpg(process.pid, sig)
            except ProcessLookupError:
                pass
            try:
                stdout, stderr = process.communicate(timeout=KILL_GRACE_SECONDS)
                if sig == signal.SIGTERM:
                    # Closed pipes and an exited leader don't mean every child exited.
                    # Allow the rest of the group its grace period, then send SIGKILL.
                    time.sleep(max(0, deadline - time.monotonic()))
                    continue
                return None, stdout, stderr
            except subprocess.TimeoutExpired:
                continue

        # Something that left the process group still holds the pipes: give up on the output
        self.log.warning("Processes of %s survived SIGKILL of their group, discarding their output", script)
        for pipe in (process.stdout, process.stderr):
            if pipe:
                pipe.close()
        process.wait()
        return None, "", ""
