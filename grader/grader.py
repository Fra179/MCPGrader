import json
from typing import Iterable
from config import ProgramConfig, AssignmentConfig, AssignmentTaskConfig
from git import Git, exc
from pathlib import Path
from shutil import rmtree
from logging import Logger
from runners import *
from .structs import TaskGradeResult
from pusher.github_pusher import GithubPusher
from grader_worker import GraderWorkerAPI, SubmissionInfo
from .grading_state import GradingState
from .submission_grader import SubmissionGrader
from .utils import deploy_key_env, write_json_atomic

class Grader:
    def __init__(self, config: ProgramConfig, logger: Logger) -> None:
        self.config = config
        self.wd = Path(config.grader.working_dir)
        self.log = logger
        self.job_ids: list[tuple[AssignmentConfig, AssignmentTaskConfig, list[int] | None]] = []  # None = skipped task
        self.runner: ABRunner = self._get_runner()
        self.grading_state = GradingState(self.wd / "grading_state.json")

    def _grade_assignment(self, assignment_cfg: AssignmentConfig) -> None:
        worker = GraderWorkerAPI(self.config.grader.worker_url, self.config.grader.worker_private_key)
        submissions = worker.get_submissions(assignment_cfg.name)
        self.log.info("Found %d submissions for assignment %s", len(submissions), assignment_cfg.name)

        if any(not task.skip for task in assignment_cfg.tasks):
            commit_hashes = self._get_latest_commit_hashes(submissions)
        else:
            commit_hashes = {}

        for task in assignment_cfg.tasks:
            blocking = task.blocking

            if task.skip:
                self.log.info("Skipping grading for task: %s[%s]", assignment_cfg.name, task.name)
                self.job_ids.append((assignment_cfg, task, None))
                continue

            updated_submissions = self._filter_updated_submissions(assignment_cfg, task, submissions, commit_hashes)
            if not updated_submissions:
                self.log.info("No updated submissions for task %s[%s], not launching a job", assignment_cfg.name, task.name)
                continue

            # Clear results left by a previous run, so only this run's job is read back
            # TODO: make runs restartable by ingesting leftover result files (from a run that
            # crashed before retrieving them) instead of deleting them.
            results_dir = self._results_dir(assignment_cfg, task)
            rmtree(results_dir, ignore_errors=True)
            results_dir.mkdir(parents=True)

            if self.config.grader.job_mode == "per_submission":
                batches = [[submission] for submission in updated_submissions]
            else:
                batches = [updated_submissions]

            self.log.info("Launching %d grading job(s) for task %s[%s] with %d updated submissions", len(batches), assignment_cfg.name, task.name, len(updated_submissions))
            # TODO: submitit pickles the arguments (including every student's deploy_key) into
            # <working_dir>/slurm_logs/*_submitted.pkl and never deletes them, deal with it
            submission_grader = SubmissionGrader(task, self.wd, assignment_cfg.preserve_repo_files, self.log.level)
            job_ids = [self.runner.run(submission_grader.grade_all, task, batch, results_dir) for batch in batches]
            self.job_ids.append((assignment_cfg, task, job_ids))

            if blocking:
                self.log.info("Waiting for blocking task %s[%s] to complete", assignment_cfg.name, task.name)
                for job_id in job_ids:
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

    def _filter_updated_submissions(self, assignment_cfg: AssignmentConfig, task: AssignmentTaskConfig, submissions: Iterable[SubmissionInfo], commit_hashes: dict[str, str | None]) -> list[SubmissionInfo]:
        updated_submissions = []

        for submission in submissions:
            if submission.ssh_url not in commit_hashes:  # empty repository
                continue

            commit_hash = commit_hashes[submission.ssh_url]
            if commit_hash is None or self.grading_state.needs_grading(assignment_cfg.name, task, submission, commit_hash):
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
           
    def _record_results(self) -> None:
        """Record every result file this run's jobs wrote into the grading state."""
        for assignment_cfg, task, job_ids in self.job_ids:
            if job_ids is None:
                self.log.info("Skipping result retrieval for skipped task: %s[%s]", assignment_cfg.name, task.name)    
                continue

            self.log.info("Collecting results for %s[%s]", assignment_cfg.name, task.name)
            for job_id in job_ids:
                try:
                    self.runner.collect_results(job_id)  # only to surface a job failure
                except Exception as e:
                    self.log.error("A grading job for %s[%s] failed, keeping the results it wrote before failing: %s", assignment_cfg.name, task.name, str(e))

            # Read once per task: all of a task's jobs write into the same folder, one file per student
            for result in self._read_results(self._results_dir(assignment_cfg, task)):
                self.grading_state.record(assignment_cfg.name, task, result)

    def _get_runner(self) -> ABRunner:
        logs_dir = Path(self.config.grader.working_dir) / "slurm_logs"
        logs_dir.mkdir(parents=True, exist_ok=True)
        logs_dir.chmod(0o700)  # submitit's pickles in here contain the students' deploy keys
        return SlurmRunner(logs_folder=str(logs_dir), max_submitted_jobs=self.config.grader.max_submitted_jobs)

    def _save_grades_file(self, data: dict) -> None:
        write_json_atomic(Path(self.config.grader.grades_file), data, indent=4)

    def grade(self):
        for assignment in self.config.assignments:
            self.log.info("Launching grading job for assignment: %s", assignment.name)
            self._grade_assignment(assignment)

        self.log.info("Waiting for all grading jobs to complete")
        self.runner.wait_all()

        self._record_results()

        # The grading state is the source of truth, the grades file is only an export of it, rewritten
        # on every run.
        self.grading_state.save()
        self._save_grades_file(self.grading_state.export_leaderboard(self.config))

        if self.config.pusher and self.config.pusher.push:
            pusher = GithubPusher(self.config.pusher)
            commit_sha = pusher.push_results(self.config.grader.grades_file)
            self.log.info("Pushed results to GitHub repository %s at commit %s", self.config.pusher.github_repo, commit_sha)