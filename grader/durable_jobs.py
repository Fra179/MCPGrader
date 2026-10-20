"""Durable scheduler intents, independent of Worker publication.

An absent allocation is evidence for a bounded retry, not proof of nonacceptance.
Keep old intents so late jobs/results can be adopted without confusing evaluations.
"""
from datetime import datetime, timezone
from uuid import uuid4

from runners.ABRunner import DefiniteSubmissionError
from runners.SlurmRunner import ACTIVE_STATES, TERMINAL_STATES
from .grading_state import _leaderboard_name


def now() -> datetime:
    return datetime.now(timezone.utc)


HOLDS = {"prepared", "submitting", "accepted", "uncertain", "blocked"}


class DurableJobs:
    def __init__(self, grader):
        self.grader = grader
        self.state = grader.grading_state
        self.runner = grader.runner
        self.log = grader.log
        self.config = grader.config.grader

    def held_students(self, assignment, task):
        return {
            student for intent in self.state.submission_intents.values()
            if intent["assignment"] == assignment and intent["task"] == task
            and intent["state"] in HOLDS for student in intent["students"]
        }

    def occupied(self):
        # A token with multiple allocations occupies all its known slots.
        intents = sum(max(1, len(i["job_ids"])) for i in self.state.submission_intents.values()
                      if i["state"] in HOLDS)
        return intents + sum("submission_token" not in j for j in self.state.jobs)

    def retained_bundles(self):
        # Presumed-absent attempts can still turn up late. Retain their input bundles.
        return {b for i in self.state.submission_intents.values()
                if i["state"] not in {"finished", "rejected"} for b in i["bundles"]}

    def ensure_run(self, assignment, task, submission, commit):
        for run in reversed(list(self.state.runs.values())):
            if (run["assignment"], run["task"], run["submission_id"], run["repo_url"],
                run["commit_hash"], run["perf_hash"], run["state"]) == (
                    assignment.name, task.name, submission.id, submission.ssh_url,
                    commit, task.performance_hash(), "active"):
                return run
        run_id = str(uuid4())
        run = {
            "run_id": run_id, "assignment": assignment.name, "task": task.name,
            "submission_id": submission.id, "student_id": str(submission.student_id),
            "repo_url": submission.ssh_url, "commit_hash": commit,
            "perf_hash": task.performance_hash(), "reduction": task.reduction or "mean",
            "state": "active", "attempts": self.state.attempt_count(
                assignment.name, str(submission.student_id), task.name, commit, task.performance_hash()),
            "recoveries": 0, "update_id": submission.update_id,
            "created_at": now().isoformat(), "worker_state": "unstarted",
            "name": _leaderboard_name(submission.ssh_url, str(submission.student_id)),
        }
        self.state.runs[run_id] = run
        # Creation and snapshot membership are one durable transition. Otherwise
        # a crash could leave a predecessor which no snapshot knows to abandon.
        for snapshot in reversed(list(self.state.snapshots.values())):
            if (not snapshot["finished"] and snapshot["assignment"] == assignment.name
                    and snapshot["submission_id"] == submission.id
                    and snapshot["repo_url"] == submission.ssh_url
                    and snapshot["commit_hash"] == commit
                    and snapshot["fingerprints"].get(task.name) == run["perf_hash"]):
                snapshot["runs"][task.name] = run_id
                run["update_id"] = snapshot["update_id"]
                break
        self.state.save()
        return run

    def submit(self, function, task, ready, results_dir, commits, bundles, runs):
        token = uuid4().hex
        context = self.runner.submission_context(task) if hasattr(self.runner, "submission_context") else {"backend": "custom"}
        intent = {
            "state": "prepared", "run_ids": [r["run_id"] for r in runs],
            "assignment": runs[0]["assignment"], "task": task.name,
            "students": [str(s.student_id) for s in ready], "perf_hash": runs[0]["perf_hash"],
            "bundles": list(bundles.values()), "submitted_at": now().isoformat(),
            "context": context, "job_ids": {}, "counted": False, "absence_since": None,
        }
        self.state.submission_intents[token] = intent
        self.state.save()
        intent["state"] = "submitting"
        self.state.save()
        run_ids = {s.ssh_url: r["run_id"] for s, r in zip(ready, runs)}
        try:
            arguments = (ready, results_dir, commits, bundles, run_ids, token)
            if hasattr(self.runner, "submission_context"):
                job_id = self.runner.submit(function, task, *arguments, submission_token=token)
            else:
                job_id = self.runner.submit(function, task, *arguments)
        except DefiniteSubmissionError as error:
            intent["state"] = "rejected"
            intent["error"] = str(error)
            self.state.save()
            self.log.error("Scheduler rejected submission %s: %s", token, error)
            return False
        except Exception as error:
            intent["state"] = "uncertain"
            intent["error"] = str(error)
            self.state.save()
            self.log.warning("Scheduler submission %s is uncertain; checking before retry: %s", token, error)
            return False
        self._adopt(token, intent, {str(job_id): "RUNNING"})
        self.state.save()
        return True

    def _adopt(self, token, intent, allocations):
        intent["job_ids"].update(allocations)
        if not intent["counted"]:
            for run_id in intent["run_ids"]:
                run = self.state.runs[run_id]
                run["attempts"] += 1
                if run["state"] == "active":
                    self.state.add_attempt(run["assignment"], run["student_id"], run["task"], run["commit_hash"], run["perf_hash"])
            intent["counted"] = True
        intent["absence_since"] = None
        intent["state"] = "accepted"
        for job_id, state in intent["job_ids"].items():
            if state in TERMINAL_STATES:
                self.state.remove_job(job_id)
            elif not any(j["job_id"] == job_id for j in self.state.jobs):
                self.state.add_job(job_id, intent["assignment"], intent["task"], intent["students"], intent["perf_hash"], intent["bundles"])
                self.state.jobs[-1]["submission_token"] = token
        if len(intent["job_ids"]) > 1:
            intent["state"] = "blocked"
            self.log.error("Multiple allocations for token %s: %s; operator reconciliation required", token, list(intent["job_ids"]))
        elif all(s in TERMINAL_STATES for s in intent["job_ids"].values()):
            intent["state"] = "finished"

    def reconcile(self):
        job_ids = [j["job_id"] for j in self.state.jobs if "submission_token" in j]
        try:
            finished = self.runner.finished_jobs(job_ids) if job_ids else {}
        except Exception as error:
            self.log.warning("Could not query scheduler: %s", error)
            # An outage interrupts the period of successful absence observations.
            for i in self.state.submission_intents.values():
                i["absence_since"] = None
            self.state.save()
            return
        for token, intent in self.state.submission_intents.items():
            status = intent["state"]
            if status in {"finished", "rejected"}:
                continue
            if status == "prepared":
                # No external call was permitted before 'submitting'. Rebuild safely next pass.
                intent["state"] = "rejected"
                continue
            if status == "accepted":
                states = {job_id: finished.get(job_id, "RUNNING") for job_id in intent["job_ids"]}
                if all(s in ACTIVE_STATES | TERMINAL_STATES for s in states.values()):
                    self._adopt(token, intent, states)
                    continue
                intent["state"] = "uncertain"
            if not hasattr(self.runner, "discover_submission"):
                self.log.error("Runner cannot reconcile uncertain token %s", token)
                continue
            try:
                found = self.runner.discover_submission(token, intent["submitted_at"], intent["context"])
            except Exception as error:
                intent["absence_since"] = None
                self.log.warning("Could not discover submission %s: %s", token, error)
                continue
            if found.allocations:
                # A partial query may reveal an old terminal record while the live
                # query failed. Only a live observation can establish a hold then.
                if not found.successful:
                    intent["absence_since"] = None
                    live = {job: state for job, state in found.allocations.items() if state in ACTIVE_STATES}
                    if live:
                        self._adopt(token, intent, live)
                    continue
                if all(s in ACTIVE_STATES | TERMINAL_STATES for s in found.allocations.values()):
                    self._adopt(token, intent, found.allocations)
                else:
                    intent["absence_since"] = None
                continue
            if not found.successful:
                intent["absence_since"] = None
                self.log.warning("Submission lookup %s inconclusive: %s", token, found.errors)
                continue
            if status in {"presumed_absent", "blocked"}:
                continue
            if intent["absence_since"] is None:
                intent["absence_since"] = now().isoformat()
            elapsed = (now() - datetime.fromisoformat(intent["absence_since"])).total_seconds()
            if elapsed < self.config.submission_grace_seconds:
                continue
            for job_id in intent["job_ids"]:
                self.state.remove_job(job_id)
            intent["state"] = "presumed_absent"
            for run_id in intent["run_ids"]:
                run = self.state.runs[run_id]
                run["recoveries"] += 1
                if run["state"] == "active" and run["recoveries"] >= self.config.max_submission_recoveries:
                    intent["state"] = "blocked"
            self.log.warning("Submission %s absent after successful lookups for %.0fs; %s", token, elapsed,
                             "recovery limit reached; operator reconciliation required" if intent["state"] == "blocked" else "allowing bounded retry")
        self.state.save()
