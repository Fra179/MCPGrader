from collections import defaultdict
import json
from typing import Iterable
from config import ProgramConfig, AssignmentConfig, AssignmentTaskConfig
from git import Git, exc
from aggregation import AGG_NAME_TO_CLASS
from pathlib import Path
from os import mkdir
from shutil import rmtree
from logging import Logger
from runners import *
from .structs import GradeResult, TaskGradeResult
from pusher.github_pusher import GithubPusher
from grader_worker import GraderWorkerAPI, SubmissionInfo
from .task_cache import TaskCache
from .submission_grader import SubmissionGrader
from .utils import deploy_key_env, write_json_atomic

class Grader:
    def __init__(self, config: ProgramConfig, logger: Logger) -> None:
        self.config = config
        self.wd = Path(config.grader.working_dir)
        self.log = logger
        self.job_ids: list[tuple] = []
        self.runner: ABRunner = self._get_runner()
        self.caches: dict[tuple[str, str], TaskCache] = {}  # by (assignment name, task name)

        if not (self.wd / ".cache").exists():
            mkdir(self.wd / ".cache")

    def _open_caches_for_assignment(self, assignment_cfg: AssignmentConfig) -> None:
        for task in assignment_cfg.tasks:
            cache_file_path = self.wd / ".cache" / f"{assignment_cfg.name}_{task.name}_cache.json"
            self.caches[(assignment_cfg.name, task.name)] = TaskCache(cache_file_path, task.performance_hash())

    def _save_caches_for_assignment(self, assignment: AssignmentConfig) -> None:
        for task in assignment.tasks:
            cache = self.caches.get((assignment.name, task.name))
            if cache is not None:
                cache.save()
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

            updated_submissions = self._filter_updated_submissions(self.caches[(assignment_cfg.name, task.name)], submissions, commit_hashes)
            if not updated_submissions:
                self.log.info("No updated submissions for task %s[%s], not launching a job", assignment_cfg.name, task.name)
                continue

            # Clear results left by a previous run, so only this run's job is read back
            # TODO: make runs restartable by ingesting leftover result files (from a run that
            # crashed before retrieving them) instead of deleting them.
            results_dir = self._results_dir(assignment_cfg, task)
            rmtree(results_dir, ignore_errors=True)
            results_dir.mkdir(parents=True)

            self.log.info("Launching grading job for task %s[%s] with %d updated submissions", assignment_cfg.name, task.name, len(updated_submissions))
            # TODO: submitit pickles the arguments (including every student's deploy_key) into
            # <working_dir>/slurm_logs/*_submitted.pkl and never deletes them, so the keys stay on disk
            # (readable only by the grader's user, see _get_runner).
            submission_grader = SubmissionGrader(task, self.wd, assignment_cfg.preserve_repo_files, self.log.level)
            job_id = self.runner.run(submission_grader.grade_all, task, updated_submissions, results_dir)
            self.job_ids.append((assignment_cfg, task, job_id))

            if blocking:
                self.log.info("Waiting for blocking task %s[%s] to complete", assignment_cfg.name, task.name)
                self.runner.wait(job_id)

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

    def _filter_updated_submissions(self, cache: TaskCache, submissions: Iterable[SubmissionInfo], commit_hashes: dict[str, str | None]) -> list[SubmissionInfo]:
        updated_submissions = []

        for submission in submissions:
            if submission.ssh_url not in commit_hashes:  # empty repository
                continue

            commit_hash = commit_hashes[submission.ssh_url]
            if commit_hash is None or not cache.is_graded(submission.ssh_url, commit_hash):
                updated_submissions.append(submission)

        return updated_submissions

    def _results_dir(self, assignment: AssignmentConfig, task: AssignmentTaskConfig) -> Path:
        return self.wd / "results" / assignment.name / task.name

    def _read_results(self, results_dir: Path) -> list[TaskGradeResult]:
        results = []
        for result_path in sorted(results_dir.glob("*.json")):
            try:
                with open(result_path, 'r') as f:
                    results.append(json.load(f))
            except (OSError, json.JSONDecodeError) as e:
                self.log.error("Failed to read result file %s: %s", result_path, str(e))
        return results
           
    def _retrieve_results(self, existing_data: dict) -> dict:
        data: dict[tuple[str, str], GradeResult] = defaultdict(lambda: GradeResult("", {}, {}, {}, {}, {}, {}, {}))

        # Start from the existing grades, so students who weren't regraded are kept
        # TODO: drop students no longer in the worker and tasks no longer in the config.
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

                # Update the cache with the latest commit hash for this student and task
                self.caches[(assignment_cfg.name, task.name)].record(result["repo_url"], result["commit_hash"])

        result = defaultdict(list)

        for (assignment_name, _), grade_result in data.items():
            result[assignment_name].append(grade_result.to_dict())
        
        return result

    def _get_runner(self) -> ABRunner:
        logs_dir = Path(self.config.grader.working_dir) / "slurm_logs"
        logs_dir.mkdir(parents=True, exist_ok=True)
        logs_dir.chmod(0o700)  # submitit's pickles in here contain the students' deploy keys
        return SlurmRunner(logs_folder=str(logs_dir))

    # TODO (no immediate need): replace the grades file and the per-task cache files with a SQLite
    # database: a results table with the perf_hash and every attempt, the leaderboard exported from
    # it, and grades plus cache saved in one transaction. Only the main process on the submitter node
    # may open it (SQLite locking is unreliable on NFS/Lustre); jobs keep writing result files.
    def _load_grades_file(self) -> dict:
        grades_file_path = Path(self.config.grader.grades_file)
        if grades_file_path.exists():
            with open(grades_file_path, 'r') as f:
                return json.load(f)
        return {}
    
    def _save_grades_file(self, data: dict) -> None:
        write_json_atomic(Path(self.config.grader.grades_file), data, indent=4)

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

        # Grades before caches: a crash in between only causes a regrade, never a cached
        # commit whose result was lost. Both before pushing, so a failed push loses nothing.
        self._save_grades_file(results)

        for assignment in self.config.assignments:
            self._save_caches_for_assignment(assignment)

        # TODO: the pushed file includes every student's full stdout and error; keep a private
        # file with the full results and push only the summary fields.
        if self.config.pusher and self.config.pusher.push:
            pusher = GithubPusher(self.config.pusher)
            commit_sha = pusher.push_results(self.config.grader.grades_file)
            self.log.info("Pushed results to GitHub repository %s at commit %s", self.config.pusher.github_repo, commit_sha)