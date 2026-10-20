import json
import time
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Iterable
from config import ProgramConfig, AssignmentConfig, AssignmentTaskConfig
from git import Git, exc
from pathlib import Path
from logging import Logger
import sentry_sdk
from runners import *
from .structs import TaskGradeResult
from pusher.github_pusher import GithubPusher
from grader_worker import GraderWorkerAPI, SubmissionInfo
from .grading_state import GradingState, JobRecord
from .durable_jobs import DurableJobs
from .submission_grader import SubmissionGrader
from .repository_staging import RepositoryStaging
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
        self.repository_staging = RepositoryStaging(self.wd)
        self.failed_downloads: set[tuple[str, str]] = set()
        self.reported_download_failures: set[tuple[str, str, str]] = set()
        self._worker_client = None
        self._discovery_updates = {}

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
        """Returns whether jobs are in flight or work is deferred by capacity or cooldown."""
        self._publication_attempts = set()
        DurableJobs(self).reconcile()
        self._drop_finished_jobs()

        # injest every result present in the folders
        self._ingest_results()
        self._flush_publications()

        deferred = 0 # work waiting for job capacity or a task's grading cooldown
        for assignment in self.config.assignments:
            deferred += self._submit_assignment(assignment)

        retained = {bundle for job in self.grading_state.jobs for bundle in job.get("bundles", [])}
        retained.update(DurableJobs(self).retained_bundles())
        self.repository_staging.cleanup(retained)
        self._flush_publications()
        self._finish_snapshots()
        self._flush_publications()
        return bool(DurableJobs(self).occupied()) or deferred > 0

    def _submit_assignment(self, assignment_cfg: AssignmentConfig) -> int:
        """Submit updated submissions; return work deferred by job capacity or cooldown."""
        if all(task.skip for task in assignment_cfg.tasks):
            self.log.info("Skipping assignment %s: all its tasks are skipped", assignment_cfg.name)
            if self.config.grader.worker_publication:
                self._snapshot(assignment_cfg)
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
            in_flight.update(DurableJobs(self).held_students(assignment_cfg.name, task.name))
            
            # submissions that should be sent out in this pass are the submissions of those students that:
            # 1. have a new commit or a changed task configuration
            # 2. are not already being graded by a job in flight
            # 3. have not already had MAX_GRADING_ATTEMPTS jobs that ended without producing their result
            updated_submissions = []
            cooling_down = 0

            for submission in self._filter_updated_submissions(assignment_cfg, task, submissions, commit_hashes):
                if str(submission.student_id) in in_flight:
                    continue

                previous = self.grading_state.records.get(assignment_cfg.name, {}).get(str(submission.student_id), {}).get(task.name)
                if previous is not None and task.grading_cooldown_seconds:
                    eligible_at = datetime.fromisoformat(previous["graded_at"]) + timedelta(seconds=task.grading_cooldown_seconds)
                    if datetime.now(timezone.utc) < eligible_at:
                        cooling_down += 1
                        self.log.info("Grading cooldown for %s[%s] student %s until %s", assignment_cfg.name, task.name, submission.student_id, eligible_at.isoformat())
                        continue

                commit_hash = commit_hashes[submission.ssh_url]

                if self._give_up_if_exhausted(assignment_cfg, task, submission, commit_hash):
                    continue

                updated_submissions.append(submission)

            deferred += cooling_down

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
            if task.blocking and (DurableJobs(self).held_students(assignment_cfg.name, task.name) or self.grading_state.task_jobs(assignment_cfg.name, task.name) or task_deferred or task_failed or cooling_down):
                blocked_by = task.name

        return deferred

    def _submit_task(self, assignment_cfg: AssignmentConfig, task: AssignmentTaskConfig, updated_submissions: list[SubmissionInfo], commit_hashes: dict[str, str | None]) -> tuple[int, int]:
        """Submit this task's updated submissions, up to max_submitted_jobs in flight. Returns how many
        jobs were deferred by that limit and how many downloads/submissions failed.
        Download failures wait for the next run; refused jobs can retry on later passes."""
        if self.config.grader.job_mode == "per_submission":
            batches = [[submission] for submission in updated_submissions]
        else:
            batches = [updated_submissions]

        # All of a task's jobs write into the same folder, one file per student
        results_dir = self._results_dir(assignment_cfg, task)
        results_dir.mkdir(parents=True, exist_ok=True)

        # Only credential-free submission copies and local bundle paths go into job pickles.
        submission_grader = SubmissionGrader(task, self.wd, assignment_cfg.preserve_repo_files, self.log.level)

        submitted = failed = processed = 0
        for batch in batches:
            if not self._has_free_job_slot():
                break
            processed += 1
            batch_commits: dict[str, str] = {}
            bundle_paths: dict[str, str] = {}
            ready = []
            for submission in batch:
                commit_hash = commit_hashes[submission.ssh_url]
                if commit_hash is None:
                    raise ValueError(f"Cannot submit {submission.repo_url} without a snapshot commit")
                download = (submission.ssh_url, commit_hash)
                if download in self.failed_downloads:
                    failed += 1
                    continue
                try:
                    bundle = self.repository_staging.stage(submission, commit_hash)
                except Exception as e:
                    self.failed_downloads.add(download)
                    self._report_download_failure(submission, "bundle", e)
                    failed += 1
                    continue
                batch_commits[submission.ssh_url] = commit_hash
                bundle_paths[submission.ssh_url] = str(bundle)
                ready.append(replace(submission, deploy_key=""))
            if not ready:
                continue
            runs = [DurableJobs(self).ensure_run(assignment_cfg, task, submission, batch_commits[submission.ssh_url]) for submission in ready]
            for run in runs:
                self._attach_run_to_snapshot(run)
            if not all(self._start_publication(run) for run in runs):
                failed += 1
                continue
            if not DurableJobs(self).submit(submission_grader.grade_all, task, ready, results_dir, batch_commits, bundle_paths, runs):
                failed += 1
                continue

            submitted += 1

        deferred = len(batches) - processed
        self.log.info("Submitted %d grading job(s) for task %s[%s] with %d updated submissions%s", submitted, assignment_cfg.name, task.name, len(updated_submissions), f", {deferred} deferred by max_submitted_jobs" if deferred else "")
        return deferred, failed

    def _report_download_failure(self, submission: SubmissionInfo, stage: str, error: Exception) -> None:
        """One Sentry event per submission/stage per run, with no exception locals or deploy keys."""
        self.log.warning("Repository %s failed for %s (student %s), skipping for now: %s", stage, submission.repo_url, submission.student_id, str(error))
        identity = (submission.assignment_name, submission.id, stage)
        if identity in self.reported_download_failures:
            return
        self.reported_download_failures.add(identity)
        with sentry_sdk.new_scope() as scope:
            scope.set_tag("assignment", submission.assignment_name)
            scope.set_tag("student_id", str(submission.student_id))
            scope.set_tag("download_stage", stage)
            scope.set_context("repository", {"url": submission.repo_url, "submission_id": submission.id, "error": str(error)})
            sentry_sdk.capture_message("Grader repository download failed", level="error")

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
        result = {
            "name": student_id,
            "commit_hash": commit_hash,
            "repo_url": submission.ssh_url,
            "perf_hash": task.performance_hash(),
            "status": "error",
            "error": f"Grading failed {attempts} times without producing a result (the job crashed, was killed or hit its time limit, or the repository couldn't be cloned). It will be retried after a new commit or a change to the task.",
            "stdout": "",
            "runtimes": [],
            "data": None,
        }
        run = DurableJobs(self).ensure_run(assignment_cfg, task, submission, commit_hash)
        self._attach_run_to_snapshot(run)
        if not self._start_publication(run):
            return True
        self._record_run_result(run, result)
        self.grading_state.save()
        return True

    def _has_free_job_slot(self) -> bool:
        max_jobs = self.config.grader.max_submitted_jobs
        return max_jobs == -1 or DurableJobs(self).occupied() < max_jobs

    def _drop_finished_jobs(self) -> None:
        """Forget the jobs that have finished, logging those that didn't complete. Their students'
        results are recorded from the files they wrote; a student without one is graded again."""
        jobs = [job for job in self.grading_state.jobs if "submission_token" not in job]
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
            worker = self._worker()
            submissions = worker.get_submissions(assignment_cfg.name)
            if self.config.grader.worker_discovery == "updates":
                submissions = self._discover_updates(assignment_cfg, submissions)
            self.log.info("Found %d submissions for assignment %s", len(submissions), assignment_cfg.name)
            # Existing unfinished snapshots already have a pinned commit. Do not
            # require another successful network lookup merely to resume them.
            current_fingerprints = {task.name: task.performance_hash() for task in assignment_cfg.tasks if not task.skip}
            pinned = {sub.ssh_url: snapshot["commit_hash"] for sub in submissions
                      for snapshot in self.grading_state.snapshots.values()
                      if not snapshot["finished"] and snapshot["assignment"] == assignment_cfg.name
                      and snapshot["submission_id"] == sub.id and snapshot["repo_url"] == sub.ssh_url
                      and (snapshot["fingerprints"] == current_fingerprints or any(
                          str(sub.student_id) in DurableJobs(self).held_students(assignment_cfg.name, task)
                          for task in snapshot["fingerprints"]))}
            commits = (self._get_latest_commit_hashes(s for s in submissions if s.ssh_url not in pinned)
                       if current_fingerprints else {sub.ssh_url: None for sub in submissions})
            commits.update(pinned)
            self._persist_snapshots(assignment_cfg, submissions, commits)
            if assignment_cfg.name in self._discovery_updates and all(value is not None for value in commits.values()):
                self.grading_state.discovery[assignment_cfg.name] = self._discovery_updates.pop(assignment_cfg.name)
                self.grading_state.save()
            self.snapshots[assignment_cfg.name] = (submissions, commits)
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
            if unresolved:
                self._persist_snapshots(assignment_cfg, unresolved, commit_hashes)
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
                except exc.GitCommandError as e:
                    if attempt + 1 == HEAD_LOOKUP_ATTEMPTS:
                        self.log.warning("Failed to get commit hash for submission %s after %d attempts, skipping until a later pass or run", submission.repo_url, HEAD_LOOKUP_ATTEMPTS)
                        commit_hashes[submission.ssh_url] = None
                        self._report_download_failure(submission, "HEAD lookup", e)
                except IndexError:
                    self.log.info("Submission %s has no commits, skipping grading", submission.repo_url)
                    break

        return commit_hashes

    def _filter_updated_submissions(self, assignment_cfg: AssignmentConfig, task: AssignmentTaskConfig, submissions: Iterable[SubmissionInfo], commit_hashes: dict[str, str | None]) -> list[SubmissionInfo]:
        updated_submissions = []

        for submission in submissions:
            persisted = next((snapshot for snapshot in self.grading_state.snapshots.values()
                              if not snapshot["finished"] and snapshot["assignment"] == assignment_cfg.name
                              and snapshot["submission_id"] == submission.id), None)
            if persisted is not None and (persisted["repo_url"] != submission.ssh_url
                                          or persisted["fingerprints"].get(task.name) != task.performance_hash()):
                continue
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
        for result_path in sorted((self.wd / "results").glob("*/*/**/*.json")):
            relative = result_path.relative_to(self.wd / "results")
            assignment_name, task_name = relative.parts[:2]
            try:
                with open(result_path) as file:
                    result: TaskGradeResult = json.load(file)
                if not isinstance(result, dict):
                    raise ValueError("Result must be an object")
                if "perf_hash" not in result:
                    self.log.warning("Discarding legacy result without perf_hash: %s", result_path)
                elif "run_id" in result:
                    run = self.grading_state.runs.get(result["run_id"])
                    token = result.get("submission_token")
                    intent = self.grading_state.submission_intents.get(token)
                    valid = (run is not None and intent is not None
                             and result["run_id"] in intent["run_ids"]
                             and (assignment_name, task_name, result.get("submission_id"), result.get("name"),
                                  result.get("repo_url"), result.get("commit_hash"), result.get("perf_hash")) == (
                                      run["assignment"], run["task"], run["submission_id"], run["student_id"],
                                      run["repo_url"], run["commit_hash"], run["perf_hash"]))
                    if not valid:
                        raise ValueError("Result does not match its persisted evaluation and submission intent")
                    if run["state"] == "active":
                        self._record_run_result(run, result)
                    else:
                        self.log.info("Ignoring late result for completed evaluation %s", result["run_id"])
                else:
                    # Preserve legacy orphan recovery, but never overwrite a newer evaluation.
                    active = any(r["assignment"] == assignment_name and r["task"] == task_name
                                 and r["student_id"] == result.get("name") for r in self.grading_state.runs.values())
                    if not active:
                        self.grading_state.record(assignment_name, task_name, result)
                    else:
                        self.log.warning("Ignoring legacy result covered by a newer evaluation: %s", result_path)
            except (OSError, ValueError, KeyError, TypeError) as error:
                self.log.error("Invalid result %s, leaving it for inspection: %s", result_path, error)
                continue
            ingested.append(result_path)
        if ingested:
            self.grading_state.save()
            for result_path in ingested:
                result_path.unlink(missing_ok=True)
            self.log.info("Recorded or reconciled %d result files", len(ingested))

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

    def _worker(self):
        if self._worker_client is None:
            self._worker_client = GraderWorkerAPI(self.config.grader.worker_url, self.config.grader.worker_private_key)
        return self._worker_client

    def _start_publication(self, run):
        if not self.config.grader.worker_publication:
            return True
        from .worker_publication import WorkerPublication
        # A predecessor's failed publication/cancellation must resolve before a
        # successor start; otherwise a legitimate remote 409 would block it.
        for other in self.grading_state.runs.values():
            if other["run_id"] == run["run_id"]:
                break
            if (other["submission_id"] == run["submission_id"] and other["task"] == run["task"]
                    and other.get("worker_state") not in {"cancelled", "completed"}
                    and not other.get("worker_obsolete")):
                return False
        ready = WorkerPublication(self).start(run)
        if run.get("recovered_result") is not None:
            self._record_run_result(run, run.pop("recovered_result"))
            return False
        return ready

    def _flush_publications(self):
        if self.config.grader.worker_publication:
            from .worker_publication import WorkerPublication
            publisher = WorkerPublication(self)
            for run in self.grading_state.runs.values():
                if run["state"] == "completed" and run.get("result") is not None and run["worker_state"] not in {"completed", "cancelled"}:
                    if self._start_publication(run):
                        publisher.enqueue(run, run["result"])
            publisher.flush()

    def _record_run_result(self, run, result):
        # The first validated result wins, even if a presumed-absent original returns later.
        if run["state"] != "active":
            return
        from .publication import build_finish_payload
        # Validate the full result even when remote publication is disabled. Local
        # storage has no Worker body limit; publication applies its limit separately.
        build_finish_payload(run["submission_id"], run["task"], run["run_id"], result,
                             reduction=run["reduction"], max_bytes=2**63 - 1)
        self.grading_state.record(run["assignment"], run["task"], result)
        run["state"] = "completed"
        run["result"] = result
        if self.config.grader.worker_publication:
            from .worker_publication import WorkerPublication
            WorkerPublication(self).enqueue(run, result)
        self.grading_state.save()

    def _persist_snapshots(self, assignment, submissions, commits):
        """Resume unfinished pinned inputs across coordinator restarts; keep credentials transient."""
        from uuid import uuid4
        fingerprints = {t.name: t.performance_hash() for t in assignment.tasks if not t.skip}
        for submission in submissions:
            previous = next((s for s in reversed(list(self.grading_state.snapshots.values()))
                             if s["assignment"] == assignment.name and s["submission_id"] == submission.id
                             and not s["finished"]), None)
            if previous is not None:
                holds = any(submission.student_id is not None and str(submission.student_id) in
                            DurableJobs(self).held_students(assignment.name, t) for t in previous["fingerprints"])
                if (previous["repo_url"] == submission.ssh_url and previous["fingerprints"] == fingerprints) or holds:
                    commits[submission.ssh_url] = previous["commit_hash"]
                    continue
                # No scheduler work can still depend on this superseded snapshot.
                for run_id in previous["runs"].values():
                    run = self.grading_state.runs[run_id]
                    if run["state"] == "active":
                        if self.config.grader.worker_publication:
                            from .worker_publication import WorkerPublication
                            WorkerPublication(self).cancel(run, "Task configuration or repository changed")
                        run["state"] = "abandoned"
                previous["finished"] = True
            commit = commits.get(submission.ssh_url)
            if commit is None and fingerprints:
                continue
            snapshot_id = str(uuid4())
            self.grading_state.snapshots[snapshot_id] = {
                "snapshot_id": snapshot_id, "assignment": assignment.name,
                "submission_id": submission.id, "student_id": str(submission.student_id),
                "repo_url": submission.ssh_url, "commit_hash": commit,
                "update_id": submission.update_id, "fingerprints": fingerprints,
                "runs": {}, "finished": False,
            }
        self.grading_state.save()

    def _attach_run_to_snapshot(self, run):
        from .grading_state import _leaderboard_name
        run.setdefault("name", _leaderboard_name(run["repo_url"], run["student_id"]))
        for snapshot in reversed(list(self.grading_state.snapshots.values())):
            if (not snapshot["finished"] and snapshot["assignment"] == run["assignment"]
                    and snapshot["submission_id"] == run["submission_id"]
                    and snapshot["repo_url"] == run["repo_url"]
                    and snapshot["commit_hash"] == run["commit_hash"]
                    and snapshot["fingerprints"].get(run["task"]) == run["perf_hash"]):
                snapshot["runs"][run["task"]] = run["run_id"]
                # Provenance belongs to the persisted snapshot, not fresh discovery.
                run["update_id"] = snapshot["update_id"]
                break
        self.grading_state.save()

    def _finish_snapshots(self):
        """Reuse published evaluations for unchanged tasks; import local legacy results without compute."""
        for snapshot in self.grading_state.snapshots.values():
            if snapshot["finished"]:
                continue
            configured = next(({t.name: t.performance_hash() for t in assignment.tasks if not t.skip}
                               for assignment in self.config.assignments if assignment.name == snapshot["assignment"]), {})
            current_submission = next((sub for sub in self.snapshots.get(snapshot["assignment"], ([], {}))[0]
                                       if sub.id == snapshot["submission_id"]), None)
            repository_changed = current_submission is not None and current_submission.ssh_url != snapshot["repo_url"]
            if configured != snapshot["fingerprints"] or repository_changed:
                if not any(snapshot["student_id"] in DurableJobs(self).held_students(snapshot["assignment"], task)
                           for task in snapshot["fingerprints"]):
                    for run_id in snapshot["runs"].values():
                        run = self.grading_state.runs[run_id]
                        if run["state"] == "active":
                            if self.config.grader.worker_publication:
                                from .worker_publication import WorkerPublication
                                WorkerPublication(self).cancel(run, "Task configuration changed during evaluation")
                            run["state"] = "abandoned"
                    snapshot["finished"] = True
                continue
            for task, fingerprint in snapshot["fingerprints"].items():
                if task in snapshot["runs"]:
                    continue
                record = self.grading_state.records.get(snapshot["assignment"], {}).get(snapshot["student_id"], {}).get(task)
                if record is None or (record["repo_url"], record["commit_hash"], record["perf_hash"]) != (
                        snapshot["repo_url"], snapshot["commit_hash"], fingerprint):
                    continue
                run = next((r for r in reversed(list(self.grading_state.runs.values()))
                            if r["assignment"] == snapshot["assignment"] and r["task"] == task
                            and r["submission_id"] == snapshot["submission_id"]
                            and (r["repo_url"], r["commit_hash"], r["perf_hash"]) == (
                                snapshot["repo_url"], snapshot["commit_hash"], fingerprint)
                            and r["state"] == "completed"), None)
                if run is not None:
                    snapshot["runs"][task] = run["run_id"]
                elif self.config.grader.worker_publication:
                    # Import only exact recorded inputs; never label an old result with a new hash.
                    from uuid import uuid4
                    from .grading_state import _leaderboard_name
                    run_id = str(uuid4())
                    reduction = next((t.reduction or "mean" for a in self.config.assignments
                                      if a.name == snapshot["assignment"] for t in a.tasks if t.name == task), "mean")
                    run = {
                        "run_id": run_id, "assignment": snapshot["assignment"], "task": task,
                        "submission_id": snapshot["submission_id"], "student_id": snapshot["student_id"],
                        "repo_url": record["repo_url"], "commit_hash": record["commit_hash"],
                        "perf_hash": fingerprint, "reduction": reduction, "state": "active",
                        "attempts": 0, "recoveries": 0, "update_id": snapshot["update_id"],
                        "worker_state": "unstarted", "name": _leaderboard_name(record["repo_url"], snapshot["student_id"]),
                        "result": dict(record, name=snapshot["student_id"]), "imported": True,
                    }
                    self.grading_state.runs[run_id] = run
                    snapshot["runs"][task] = run_id
                    self.grading_state.save()
            # Imported grades require publication but never enter the scheduler.
            for run_id in snapshot["runs"].values():
                run = self.grading_state.runs[run_id]
                if run.get("imported") and run["state"] == "active" and self._start_publication(run):
                    self._record_run_result(run, run["result"])
            computed = all(task in snapshot["runs"] and self.grading_state.runs[snapshot["runs"][task]]["state"] == "completed"
                           for task in snapshot["fingerprints"])
            if not self.config.grader.worker_publication:
                # Local legacy results are sufficient when no remote evidence is required.
                computed = all((r := self.grading_state.records.get(snapshot["assignment"], {}).get(snapshot["student_id"], {}).get(task))
                               and (r["repo_url"], r["commit_hash"], r["perf_hash"]) == (snapshot["repo_url"], snapshot["commit_hash"], fp)
                               for task, fp in snapshot["fingerprints"].items())
            published = all(self.grading_state.runs[r]["worker_state"] == "completed" for r in snapshot["runs"].values())
            if computed and (not self.config.grader.worker_publication or published):
                if self.config.grader.worker_publication and snapshot["update_id"] is not None:
                    from .worker_publication import WorkerPublication
                    WorkerPublication(self).enqueue_ack(snapshot)
                snapshot["finished"] = True
        self.grading_state.save()

    def _discover_updates(self, assignment, submissions):
        """Use webhooks as a hint; new/changed tasks and periodic scans remain authoritative."""
        fingerprints = {task.name: task.performance_hash() for task in assignment.tasks if not task.skip}
        prior = self.grading_state.discovery.get(assignment.name)
        current_time = datetime.now(timezone.utc)
        full = (prior is None or prior["fingerprints"] != fingerprints or
                (current_time - datetime.fromisoformat(prior["last_full_scan"])).total_seconds()
                >= self.config.grader.worker_reconcile_seconds)
        updates = {submission.id: submission for submission in self._worker().get_updates(assignment.name)}
        unfinished = {s["submission_id"] for s in self.grading_state.snapshots.values()
                      if s["assignment"] == assignment.name and not s["finished"]}
        selected = []
        for submission in submissions:
            pending = updates.get(submission.id)
            if pending is not None:
                submission = replace(submission, update_id=pending.update_id)
            unknown = str(submission.student_id) not in self.grading_state.records.get(assignment.name, {})
            if full or pending is not None or submission.id in unfinished or unknown:
                selected.append(submission)
        if full:
            self._discovery_updates[assignment.name] = {
                "fingerprints": fingerprints, "last_full_scan": current_time.isoformat(),
            }
        return selected
