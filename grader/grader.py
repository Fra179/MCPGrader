import json
import time
from datetime import datetime, timedelta, timezone
from typing import Iterable
from config import ProgramConfig, AssignmentConfig, AssignmentTaskConfig
from git import Git, exc
from pathlib import Path
from logging import Logger
from runners import *
from .structs import TaskGradeResult
from pusher.github_pusher import GithubPusher
from grader_worker import GraderWorkerAPI, SubmissionInfo
from .grading_state import GradingState, JobRecord
from .submission_grader import SubmissionGrader
from .utils import LockHeldError, deploy_key_env, exclusive_lock, write_json_atomic

# How long a job the scheduler doesn't know about yet (sacct can lag right after sbatch) still counts as in flight
UNKNOWN_JOB_GRACE = timedelta(minutes=10)
# Jobs a submission gets, for the same commit and task configuration, before it's given an error
# result instead of another job (each job that ends without its result counts)
MAX_GRADING_ATTEMPTS = 3
HEAD_LOOKUP_ATTEMPTS = 3

class Grader:
    """Grades in passes. Each pass catches up with what happened since the last one: it drops finished
    jobs, records the result files they wrote and submits jobs for submissions that need grading (up
    to max_submitted_jobs in flight). The run publishes the leaderboard when it ends. Nothing lives only in this process:
    a run can be killed at any point and the next one carries on. Only one run at a time per working
    directory: it holds <working_dir>/grader.lock for its whole duration."""

    def __init__(self, config: ProgramConfig, logger: Logger) -> None:
        self.config = config
        self.wd = Path(config.grader.working_dir)
        self.log = logger

        self.runner: ABRunner = self._get_runner()
        
        self.grading_state: GradingState  # loaded once the lock is held
        self.poll_interval = 30.0
        
        # Successful HEAD lookups stay fixed for the run; unresolved ones can be retried.
        self.snapshots: dict[str, tuple[list[SubmissionInfo], dict[str, str | None]]] = {}

    def grade(self):
        """One pass, or with wait_for_jobs, passes until no job is in flight and nothing is left to submit."""
        try:
            with exclusive_lock(self.wd / "grader.lock"):
                self.grading_state = GradingState(self.wd / "grading_state.json")
                self._grade_locked()
        except LockHeldError as e:
            self.log.info("Another grader run is in progress, exiting: %s", str(e))

    def _grade_locked(self) -> None:
        while True:
            pending = self._pass()
            if not pending or not self.config.grader.wait_for_jobs:
                break
            self.log.info("%d grading jobs in flight, next pass in %ds", len(self.grading_state.jobs), self.poll_interval)
            time.sleep(self.poll_interval)

        # note, there could be a rare event in which there is a deterministic crash (due to SLURM killing the job) of the grader always before the 
        # _publish() function, in that case go to Lourdes and seek assistance from the madonna herself
        self._publish()

    def _pass(self) -> bool:
        """Returns whether work remains: jobs in flight, or jobs not submitted because of max_submitted_jobs."""
        self._drop_finished_jobs()

        # injest every result present in the folders
        self._ingest_results()

        deferred = 0 # due to max_submitted_jobs these are jobs that are not yet submitted, shouldn't happen on CINECA but could happen on DI cluster
        for assignment in self.config.assignments:
            deferred += self._submit_assignment(assignment)

        return bool(self.grading_state.jobs) or deferred > 0

    def _submit_assignment(self, assignment_cfg: AssignmentConfig) -> int:
        """Submit jobs for this assignment's updated submissions; returns how many were deferred by max_submitted_jobs."""
        if all(task.skip for task in assignment_cfg.tasks):
            self.log.info("Skipping assignment %s: all its tasks are skipped", assignment_cfg.name)
            return 0

        submissions, commit_hashes = self._snapshot(assignment_cfg)
        blocked_by = None  # a blocking task with work in flight: later tasks wait for a later pass
        deferred = 0

        for task in assignment_cfg.tasks:
            if task.skip:
                self.log.info("Skipping grading for task: %s[%s]", assignment_cfg.name, task.name)
                continue

            # jobs for this task of this assignment that are already running
            in_flight = {student for job in self.grading_state.task_jobs(assignment_cfg.name, task.name) for student in job["students"]}
            
            # submissions that should be sent out in this pass are the submissions of those students that:
            # 1. have a new commit or a changed task configuration
            # 2. are not already being graded by a job in flight
            # 3. have not already had MAX_GRADING_ATTEMPTS jobs that ended without producing their result
            updated_submissions = []

            for submission in self._filter_updated_submissions(assignment_cfg, task, submissions, commit_hashes):
                if str(submission.student_id) in in_flight:
                    continue

                commit_hash = commit_hashes[submission.ssh_url]

                if self._give_up_if_exhausted(assignment_cfg, task, submission, commit_hash):
                    continue

                updated_submissions.append(submission)

            if blocked_by is not None:
                if updated_submissions:
                    self.log.info("%d updated submissions for task %s[%s] wait for blocking task %s", len(updated_submissions), assignment_cfg.name, task.name, blocked_by)
                continue

            task_deferred = task_failed = 0
            if updated_submissions:
                task_deferred, task_failed = self._submit_task(assignment_cfg, task, updated_submissions, commit_hashes)
                deferred += task_deferred
            else:
                self.log.info("No updated submissions for task %s[%s]%s", assignment_cfg.name, task.name, f" ({len(in_flight)} in flight)" if in_flight else "")

            # Jobs that failed to submit hold back later tasks too, until a later run submits them
            if task.blocking and (self.grading_state.task_jobs(assignment_cfg.name, task.name) or task_deferred or task_failed):
                blocked_by = task.name

        return deferred

    def _submit_task(self, assignment_cfg: AssignmentConfig, task: AssignmentTaskConfig, updated_submissions: list[SubmissionInfo], commit_hashes: dict[str, str | None]) -> tuple[int, int]:
        """Submit this task's updated submissions, up to max_submitted_jobs in flight. Returns how many
        jobs were deferred by that limit and how many failed to submit; both are left to later passes."""
        if self.config.grader.job_mode == "per_submission":
            batches = [[submission] for submission in updated_submissions]
        else:
            batches = [updated_submissions]

        # All of a task's jobs write into the same folder, one file per student
        results_dir = self._results_dir(assignment_cfg, task)
        results_dir.mkdir(parents=True, exist_ok=True)

        # TODO: submitit pickles the arguments (including every student's deploy_key) into
        # <working_dir>/slurm_logs/*_submitted.pkl and never deletes them, deal with it
        submission_grader = SubmissionGrader(task, self.wd, assignment_cfg.preserve_repo_files, self.log.level)

        submitted = failed = 0
        for batch in batches:
            if not self._has_free_job_slot():
                break
            batch_commits: dict[str, str] = {}
            for submission in batch:
                commit_hash = commit_hashes[submission.ssh_url]
                if commit_hash is None:
                    raise ValueError(f"Cannot submit {submission.repo_url} without a snapshot commit")
                batch_commits[submission.ssh_url] = commit_hash
            try:
                job_id = self.runner.submit(submission_grader.grade_all, task, batch, results_dir, batch_commits)
            except Exception as e:
                # any reason for the failure that's dependent on slurm or the cluster will be tried again later
                self.log.error("Failed to submit a grading job for %s[%s]: %s", assignment_cfg.name, task.name, str(e))
                failed += 1
                continue
            students = [str(submission.student_id) for submission in batch]
            self.grading_state.add_job(job_id, assignment_cfg.name, task.name, students, submission_grader.perf_hash)
            for submission in batch:
                self.grading_state.add_attempt(assignment_cfg.name, str(submission.student_id), task.name, commit_hashes[submission.ssh_url], submission_grader.perf_hash)
            self.grading_state.save()  # right away: a run killed now must still know this job exists
            submitted += 1

        deferred = len(batches) - submitted - failed
        self.log.info("Submitted %d grading job(s) for task %s[%s] with %d updated submissions%s", submitted, assignment_cfg.name, task.name, len(updated_submissions), f", {deferred} deferred by max_submitted_jobs" if deferred else "")
        return deferred, failed

    def _give_up_if_exhausted(self, assignment_cfg: AssignmentConfig, task: AssignmentTaskConfig, submission: SubmissionInfo, commit_hash: str | None) -> bool:
        """If this submission already had MAX_GRADING_ATTEMPTS jobs that ended without its result, record
        an error result instead of submitting another one, and return True. A new commit or a change
        to the task resets the count."""
        student_id = str(submission.student_id)
        attempts = self.grading_state.attempt_count(assignment_cfg.name, student_id, task.name, commit_hash, task.performance_hash())
        if attempts < MAX_GRADING_ATTEMPTS:
            return False

        if commit_hash is None:
            # Nothing to attach an error result to (it would never match the commit, so it would be recorded again on every pass): just stop until git ls-remote works again
            self.log.warning("Not grading %s[%s] for student %s: %d attempts failed and its latest commit is unknown", assignment_cfg.name, task.name, student_id, attempts)
            return True

        self.log.error("Giving up on %s[%s] for student %s at commit %s: %d grading jobs ended without a result", assignment_cfg.name, task.name, student_id, commit_hash, attempts)
        self.grading_state.record(assignment_cfg.name, task.name, {
            "name": student_id,
            "commit_hash": commit_hash,
            "repo_url": submission.ssh_url,
            "perf_hash": task.performance_hash(),
            "status": "error",
            "error": f"Grading failed {attempts} times without producing a result (the job crashed, was killed or hit its time limit, or the repository couldn't be cloned). It will be retried after a new commit or a change to the task.",
            "stdout": "",
            "runtimes": [],
            "data": None,
        })
        self.grading_state.save()
        return True

    def _has_free_job_slot(self) -> bool:
        max_jobs = self.config.grader.max_submitted_jobs
        return max_jobs == -1 or len(self.grading_state.jobs) < max_jobs

    def _drop_finished_jobs(self) -> None:
        """Forget the jobs that have finished, logging those that didn't complete. Their students'
        results are recorded from the files they wrote; a student without one is graded again."""
        jobs = self.grading_state.jobs
        if not jobs:
            return

        try:
            finished = self.runner.finished_jobs([job["job_id"] for job in jobs])
        except Exception as e:
            self.log.warning("Couldn't get the state of the grading jobs, assuming they're all still running: %s", str(e))
            return

        now = datetime.now(timezone.utc)
        dropped = 0
        for job in jobs:
            state = finished.get(job["job_id"])

            # if the scheduler didn't update the job's state yet, let's wait a little bit
            if state is None or (state == "UNKNOWN" and now - datetime.fromisoformat(job["submitted_at"]) < UNKNOWN_JOB_GRACE):
                continue

            # the job terminated in a "non graceful" way: some students may not be graded (not a problem, we deal with it later but we log the thing anyway)
            if state not in ("COMPLETED", "FINISHED"):
                self._log_unfinished_job(job, state)

            self.grading_state.remove_job(job["job_id"])
            dropped += 1

        if dropped:
            self.log.info("Dropped %d finished grading job(s)", dropped)
            self.grading_state.save()

    def _log_unfinished_job(self, job: JobRecord, state: str) -> None:
        # Results the job wrote before dying are recorded anyway; students without one get a new job
        if len(job["students"]) == 1:
            self.log.error("Grading job %s for %s[%s] (student %s) ended with state %s; if it didn't write the student's result, they'll get a new job", job["job_id"], job["assignment"], job["task"], job["students"][0], state)
        else:
            self.log.error("Grading job %s for %s[%s] (students %s) ended with state %s; students it already graded keep their results, the others will get a new job", job["job_id"], job["assignment"], job["task"], ", ".join(job["students"]), state)

    def _snapshot(self, assignment_cfg: AssignmentConfig) -> tuple[list[SubmissionInfo], dict[str, str | None]]:
        if assignment_cfg.name not in self.snapshots:
            worker = GraderWorkerAPI(self.config.grader.worker_url, self.config.grader.worker_private_key)
            submissions = worker.get_submissions(assignment_cfg.name)
            self.log.info("Found %d submissions for assignment %s", len(submissions), assignment_cfg.name)
            self.snapshots[assignment_cfg.name] = (submissions, self._get_latest_commit_hashes(submissions))
        else:
            submissions, commit_hashes = self.snapshots[assignment_cfg.name]
            unresolved = [
                submission for submission in submissions
                if submission.ssh_url in commit_hashes and commit_hashes[submission.ssh_url] is None
            ]
            # retry only failed lookups to avoid a successful lookup from moving to a newer HEAD.
            for submission in unresolved:
                commit_hashes.pop(submission.ssh_url)
            commit_hashes.update(self._get_latest_commit_hashes(unresolved))
        return self.snapshots[assignment_cfg.name]

    def _get_latest_commit_hash(self, submission: SubmissionInfo) -> str:
        with deploy_key_env(submission.deploy_key) as env:
            git = Git()  # fresh instance so the per-submission env isn't shared
            with git.custom_environment(**env):
                return git.ls_remote(submission.ssh_url, "HEAD").split()[0]

    def _get_latest_commit_hashes(self, submissions: Iterable[SubmissionInfo]) -> dict[str, str | None]:
        """HEAD commit of each non-empty submission, keyed by ssh_url; None if it couldn't be fetched."""
        commit_hashes: dict[str, str | None] = {}

        for submission in submissions:
            for attempt in range(HEAD_LOOKUP_ATTEMPTS):
                try:
                    commit_hashes[submission.ssh_url] = self._get_latest_commit_hash(submission)
                    break
                except exc.GitCommandError:
                    if attempt + 1 == HEAD_LOOKUP_ATTEMPTS:
                        self.log.warning("Failed to get commit hash for submission %s after %d attempts, skipping until a later pass or run", submission.repo_url, HEAD_LOOKUP_ATTEMPTS)
                        commit_hashes[submission.ssh_url] = None
                except IndexError:
                    self.log.info("Submission %s has no commits, skipping grading", submission.repo_url)
                    break

        return commit_hashes

    def _filter_updated_submissions(self, assignment_cfg: AssignmentConfig, task: AssignmentTaskConfig, submissions: Iterable[SubmissionInfo], commit_hashes: dict[str, str | None]) -> list[SubmissionInfo]:
        updated_submissions = []

        for submission in submissions:
            if submission.ssh_url not in commit_hashes:  # empty repository
                continue

            commit_hash = commit_hashes[submission.ssh_url]
            if commit_hash is not None and self.grading_state.needs_grading(assignment_cfg.name, task, submission, commit_hash):
                updated_submissions.append(submission)

        return updated_submissions

    def _results_dir(self, assignment: AssignmentConfig, task: AssignmentTaskConfig) -> Path:
        return self.wd / "results" / assignment.name / task.name

    def _ingest_results(self) -> None:
        """Record every result file under <working_dir>/results into the grading state, save it, then
        delete the files. Whoever wrote them: jobs still running, or
        jobs of an earlier run that was killed. A crash between saving and deleting only means the
        same results are recorded again next time."""

        ingested = []
        # results/<assignment>/<task>/<student_id>.json; "*.json" skips write_json_atomic's .tmp files
        for result_path in sorted((self.wd / "results").glob("*/*/*.json")):
            task_name, assignment_name = result_path.parent.name, result_path.parent.parent.name
            try:
                with open(result_path, 'r') as f:
                    result: TaskGradeResult = json.load(f)
            except (OSError, json.JSONDecodeError) as e:
                self.log.error("Failed to read result file %s, leaving it in place: %s", result_path, str(e))
                continue

            if "perf_hash" not in result:  # perf_hash MUST be present
                self.log.warning("Result file %s has no perf_hash, discarding it", result_path)
            else:
                self.grading_state.record(assignment_name, task_name, result)
            ingested.append(result_path)

        if not ingested:
            return

        self.grading_state.save()
        for result_path in ingested:
            result_path.unlink(missing_ok=True) # delete the ingested result JUST after the grading state was saved, so a crash doesn't lose it
        self.log.info("Recorded %d result files", len(ingested))

    def _publish(self) -> None:
        """Write the grades file and push it."""
        # The grading state is the source of truth, the grades file is only an export of it
        self._save_grades_file(self.grading_state.export_leaderboard(self.config))

        if self.config.pusher and self.config.pusher.push:
            pusher = GithubPusher(self.config.pusher)
            commit_sha = pusher.push_results(self.config.grader.grades_file)
            if commit_sha is None:
                self.log.info("Leaderboard in %s is already up to date", self.config.pusher.github_repo)
            else:
                self.log.info("Pushed results to GitHub repository %s at commit %s", self.config.pusher.github_repo, commit_sha)

    def _get_runner(self) -> ABRunner:
        logs_dir = Path(self.config.grader.working_dir) / "slurm_logs"
        logs_dir.mkdir(parents=True, exist_ok=True)
        logs_dir.chmod(0o700)  # submitit's pickles in here contain the students' deploy keys
        return SlurmRunner(logs_folder=str(logs_dir))

    def _save_grades_file(self, data: dict) -> None:
        write_json_atomic(Path(self.config.grader.grades_file), data, indent=4)
