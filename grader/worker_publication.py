"""Durable Worker lifecycle writes, independent of scheduler execution."""

from copy import deepcopy
from typing import Any

from grader_worker import GraderWorkerException
from .publication import PublicationPayloadError, build_finish_payload


class WorkerPublication:
    def __init__(self, grader):
        self.grader = grader

    @property
    def state(self):
        return self.grader.grading_state

    @property
    def enabled(self) -> bool:
        return self.grader.config.grader.worker_publication

    def _error(self, run: dict[str, Any], error: Exception | str, blocked: bool = False) -> None:
        run["publication_error"] = str(error)
        if blocked:
            run["publication_blocked"] = True
        self.state.save()
        self.grader.log.error("Worker publication for run %s: %s", run["run_id"], error)

    def _attempt(self, kind: str, key: str) -> bool:
        """Limit network retries to one operation per reconciliation pass when supplied."""
        attempts = getattr(self.grader, "_publication_attempts", None)
        if attempts is None:
            return True
        identity = (kind, key)
        if identity in attempts:
            return False
        attempts.add(identity)
        return True

    def start(self, run: dict[str, Any]) -> bool:
        """Persist a start before sending it; return whether scheduling is permitted."""
        if not self.enabled:
            return True
        if run.get("worker_obsolete") or run.get("publication_blocked"):
            return False
        if run.get("worker_state") == "active":
            return True
        if run.get("worker_state") == "cancelled":
            return False
        if not self._attempt("start", run["run_id"]):
            return False
        try:
            api = self.grader._worker()
            if run.get("worker_state") == "completed":
                if not run.get("result") and not run.get("recovered_result"):
                    self._recover_completed(run, api.inspect_run(run["run_id"]))
                return False
            if "start_payload" not in run:
                inspection = api.inspect_submission(run["submission_id"])
                task = inspection.get("tasks", {}).get(run["task"], {})
                predecessor = task.get("latest_run_id")
                run["start_payload"] = {
                    "id": run["submission_id"], "task": run["task"], "run_id": run["run_id"],
                    "previous_run_id": predecessor, "update_id": run.get("update_id"),
                    "name": run.get("name", str(run["student_id"])),
                    "commit_hash": run["commit_hash"], "perf_hash": run["perf_hash"],
                }
                run["worker_state"] = "unstarted"
                self.state.save()
            response = api.start_run(run["start_payload"])
            remote_state = response.get("state")
            if remote_state not in {"active", "completed", "cancelled"}:
                self._error(run, "Start response has an unknown state", blocked=True)
                return False
            run["worker_state"] = remote_state
            run["worker_revision"] = response.get("revision")
            run.pop("publication_error", None)
            self.state.save()
            if remote_state == "completed" and not run.get("result"):
                self._recover_completed(run, api.inspect_run(run["run_id"]))
            return remote_state == "active"
        except GraderWorkerException as exc:
            if exc.status_code == 404:
                run["worker_obsolete"] = True
            self._error(run, exc, blocked=exc.status_code in {400, 401, 403, 409, 413, 422})
            return False

    def _recover_completed(self, run: dict[str, Any], inspection: dict[str, Any]) -> None:
        grading = inspection.get("grading")
        if not inspection.get("payload_available", grading is not None) or not isinstance(grading, dict):
            self._error(run, "Completed Worker run payload is unavailable; local result cannot be recovered", blocked=True)
            return
        if grading.get("commit_hash") != run["commit_hash"]:
            self._error(run, "Completed Worker run has a different pinned commit", blocked=True)
            return
        # Coordinator records this through its usual result path before scheduling again.
        run["recovered_result"] = {
            **deepcopy(grading), "name": str(run["student_id"]), "repo_url": run["repo_url"],
            "perf_hash": run["perf_hash"], "submission_id": run["submission_id"], "run_id": run["run_id"],
        }
        run.pop("publication_error", None)
        self.state.save()

    def enqueue(self, run: dict[str, Any], result: dict[str, Any]) -> None:
        """Keep the first result and immutable finish body durable before file deletion."""
        if not self.enabled:
            return
        run.setdefault("result", deepcopy(result))
        if run.get("worker_state") == "completed" or run.get("worker_obsolete"):
            self.state.save()
            return
        existing = self.state.outbox.get(run["run_id"])
        if existing is not None:
            if existing["kind"] != "finish":
                self._error(run, "Completion conflicts with a pending terminal operation", blocked=True)
            else:
                self.state.save()
            return
        try:
            if run["result"]["commit_hash"] != run["commit_hash"]:
                raise PublicationPayloadError("Result commit differs from immutable run start")
            payload = build_finish_payload(run["submission_id"], run["task"], run["run_id"],
                                           run["result"], reduction=run.get("reduction") or "mean")
        except PublicationPayloadError as exc:
            self._error(run, exc, blocked=True)
            return
        self.state.outbox[run["run_id"]] = {"kind": "finish", "payload": payload}
        self.state.save()

    def cancel(self, run: dict[str, Any], reason: str) -> None:
        """Persist cancellation without making assumptions about scheduler state."""
        if not self.enabled or run.get("worker_state") in {"completed", "cancelled"}:
            return
        if not reason or len(reason) > 512:
            raise ValueError("Cancellation reason must contain 1 to 512 characters")
        existing = self.state.outbox.get(run["run_id"])
        if existing is not None:
            if existing["kind"] != "cancel":
                self._error(run, "Cancellation conflicts with a pending completion", blocked=True)
            return
        self.state.outbox[run["run_id"]] = {
            "kind": "cancel", "payload": {"id": run["submission_id"], "task": run["task"],
                                           "run_id": run["run_id"], "reason": reason},
        }
        self.state.save()

    def enqueue_ack(self, snapshot: dict[str, Any]) -> None:
        """Persist the coordinator's verified evidence without changing its provenance."""
        if not self.enabled or snapshot.get("update_id") is None or snapshot.get("acknowledgement_state") == "acknowledged":
            return
        key = "ack:" + snapshot["snapshot_id"]
        if key not in self.state.outbox:
            self.state.outbox[key] = {
                "kind": "ack", "snapshot_id": snapshot["snapshot_id"],
                "payload": {"id": snapshot["submission_id"], "update_id": snapshot["update_id"],
                            "runs": deepcopy(snapshot["runs"])},
            }
        snapshot["acknowledgement_state"] = "pending"
        self.state.save()

    def _flush_ack(self, key: str, entry: dict[str, Any]) -> None:
        snapshot = next((item for item in self.state.snapshots.values()
                         if item.get("snapshot_id") == entry["snapshot_id"]), None)
        try:
            response = self.grader._worker().acknowledge_update(entry["payload"])
            reason = response.get("reason")
            if reason == "active_runs":
                return
            if reason not in {"acknowledged", "already_acknowledged", "superseded"}:
                raise GraderWorkerException("Unknown acknowledgement disposition", status_code=400)
        except GraderWorkerException as exc:
            entry["error"] = str(exc)
            if exc.status_code == 404:
                if snapshot is not None:
                    snapshot["acknowledgement_state"] = "obsolete"
                del self.state.outbox[key]
            elif exc.status_code in {400, 401, 403, 409, 413, 422}:
                entry["blocked"] = True
            self.state.save()
            self.grader.log.error("Worker acknowledgement %s: %s", key, exc)
            return
        if snapshot is not None:
            snapshot["acknowledgement_state"] = "acknowledged"
            snapshot["acknowledgement_disposition"] = reason
        del self.state.outbox[key]
        self.state.save()

    def flush(self) -> None:
        """Retry transient failures on later passes; retain blocked payloads for inspection."""
        if not self.enabled:
            return
        for key, entry in list(self.state.outbox.items()):
            if entry.get("blocked"):
                continue
            if entry["kind"] == "ack":
                if self._attempt("ack", key):
                    self._flush_ack(key, entry)
                continue
            run = self.state.runs.get(key)
            if run is None or run.get("worker_obsolete"):
                continue
            if run.get("publication_blocked"):
                continue
            if run.get("worker_state") == "completed":
                if entry["kind"] == "cancel":
                    # A confirmed terminal completion already cleared this run's
                    # active state; cancellation can no longer change that result.
                    run["cancellation_disposition"] = "already_completed"
                del self.state.outbox[key]
                self.state.save()
                continue
            if run.get("worker_state") not in {"active", "cancelled"} and not self.start(run):
                continue
            if not self._attempt(entry["kind"], key):
                continue
            try:
                api = self.grader._worker()
                if entry["kind"] == "finish":
                    response = api.finish_run(entry["payload"])
                    if response.get("state") != "completed":
                        self._error(run, "Finish response does not confirm completion", blocked=True)
                        continue
                    run["worker_state"] = "completed"
                elif entry["kind"] == "cancel":
                    response = api.cancel_run(entry["payload"])
                    if response.get("state") != "cancelled":
                        self._error(run, "Cancel response does not confirm cancellation", blocked=True)
                        continue
                    run["worker_state"] = "cancelled"
                else:
                    self._error(run, "Unknown outbox operation", blocked=True)
                    continue
            except GraderWorkerException as exc:
                if exc.status_code == 404:
                    run["worker_obsolete"] = True
                    del self.state.outbox[key]
                if exc.status_code in {400, 401, 403, 409, 413, 422}:
                    entry["blocked"] = True
                self._error(run, exc, blocked=entry.get("blocked", False))
                continue
            run.pop("publication_error", None)
            del self.state.outbox[key]
            self.state.save()
