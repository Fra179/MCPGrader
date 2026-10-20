from .ABRunner import ABRunner, DefiniteSubmissionError, SubmissionDiscovery
from config.configs import AssignmentTaskConfig
import os
import getpass
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
import subprocess
import submitit
from submitit import Job
from typing import Any, Callable

# sacct states of a job that hasn't finished yet; any other state is final. A state missing from
# this list makes the grader stop tracking the job early, which at worst grades a submission twice
ACTIVE_STATES = {
    "PENDING", "CONFIGURING", "RUNNING", "COMPLETING", "SUSPENDED", "REQUEUED", "REQUEUE_FED",
    "REQUEUE_HOLD", "RESIZING", "SIGNALING", "STAGE_OUT", "STOPPED",
}

TERMINAL_STATES = {
    "COMPLETED", "FINISHED", "FAILED", "CANCELLED", "TIMEOUT", "NODE_FAIL", "OUT_OF_MEMORY",
    "BOOT_FAIL", "DEADLINE", "PREEMPTED", "REVOKED", "SPECIAL_EXIT",
}


class SlurmRunner(ABRunner):
    """Submits jobs through submitit and reads their states back with sacct, so a later process can
    tell when an earlier one's jobs are done. Without SLURM, submitit runs jobs as local processes."""

    def __init__(self, logs_folder: str = "./logs"):
        super().__init__()
        self.logs_folder = logs_folder
        self.cluster = submitit.AutoExecutor(folder=logs_folder).cluster
        self.jobs: dict[str, Job] = {}  # submitted by this process

    def submit(self, grading_function: Callable[..., Any], task: AssignmentTaskConfig, *args,
               submission_token: str | None = None, **kwargs) -> str:
        config = task.slurm_backend.config.copy()

        if submission_token is not None:
            config["slurm_job_name"] = self._job_name(submission_token)
        elif not config.get("slurm_job_name"):
            config["slurm_job_name"] = f"grading_{task.name}"

        config["slurm_use_srun"] = False  # We are already running inside a SLURM job

        # create a new executor as not to carry over previous configs
        # task only supplies the SLURM parameters; the function gets just args/kwargs
        try:
            executor = submitit.AutoExecutor(folder=self.logs_folder, cluster=self.cluster)
            executor.update_parameters(**config)
        except Exception as exc:
            raise DefiniteSubmissionError(str(exc)) from exc
        try:
            job = executor.submit(grading_function, *args, **kwargs)
        except Exception as exc:
            if self._definite_rejection(exc):
                raise DefiniteSubmissionError("SLURM rejected submission before acceptance") from exc
            raise
        self.jobs[job.job_id] = job
        if submission_token is not None and self.cluster != "slurm":
            receipt = self._receipt_path(submission_token)
            receipt.parent.mkdir(parents=True, exist_ok=True)
            temporary = receipt.with_suffix(".tmp")
            temporary.write_text(json.dumps({"job_id": job.job_id, "process_start": self._process_start(job.job_id)}))
            os.replace(temporary, receipt)
        return job.job_id

    @staticmethod
    def _definite_rejection(error: Exception) -> bool:
        """Recognize submitit's explicit sbatch rejection, preserving other uncertainty.

        CommandFunction wraps CalledProcessError in FailedJobError. Signals and
        transport errors do not establish rejection, nor does any acceptance output.
        """
        seen = set()
        while error is not None and id(error) not in seen:
            seen.add(id(error))
            if isinstance(error, subprocess.CalledProcessError):
                command = error.cmd
                executable = command[0] if isinstance(command, (list, tuple)) and command else command
                if not isinstance(executable, (str, bytes, os.PathLike)):
                    return False
                output = (error.output or "", error.stderr or "")
                accepted = any(
                    (b"submitted batch job" in value.lower() if isinstance(value, bytes)
                     else "submitted batch job" in str(value).lower()) for value in output
                )
                return os.fsdecode(os.path.basename(executable)) == "sbatch" and error.returncode > 0 and not accepted
            error = error.__cause__
        return False

    @staticmethod
    def _job_name(token: str) -> str:
        if not re.fullmatch(r"[0-9a-fA-F]{32}", token):
            raise DefiniteSubmissionError("submission_token must be a UUID hex string")
        return f"gw-{token}"

    def _receipt_path(self, token: str) -> Path:
        self._job_name(token)
        return Path(self.logs_folder) / "submission_receipts" / f"{token}.json"

    @staticmethod
    def _process_start(job_id: str) -> str | None:
        try:
            # comm may contain spaces and parentheses; fields after the final ')' are stable.
            return Path(f"/proc/{int(job_id)}/stat").read_text().rsplit(")", 1)[1].split()[19]
        except (OSError, ValueError, IndexError):
            return None

    def submission_context(self, task: AssignmentTaskConfig) -> dict:
        config = task.slurm_backend.config
        extra = config.get("slurm_additional_parameters") or {}
        context = {"backend": self.cluster, "user": getpass.getuser()}
        for key in ("account", "partition"):
            value = config.get(f"slurm_{key}", extra.get(key))
            if value:
                context[key] = str(value)
        cluster = extra.get("clusters") or os.environ.get("SLURM_CLUSTERS") or os.environ.get("SLURM_CLUSTER_NAME")
        if cluster:
            context["cluster"] = str(cluster)
        return context

    def discover_submission(self, submission_token: str, submitted_at: str,
                            context: dict) -> SubmissionDiscovery:
        name = self._job_name(submission_token)
        if context.get("backend", self.cluster) != "slurm":
            try:
                receipt = json.loads(self._receipt_path(submission_token).read_text())
                job_id = str(receipt["job_id"])
                previous_start = receipt.get("process_start")
                current_start = self._process_start(job_id)
                if previous_start and current_start and current_start != previous_start:
                    return SubmissionDiscovery({job_id: "FINISHED"})
                state = self._finished_local_jobs([job_id]).get(job_id, "RUNNING")
                return SubmissionDiscovery({job_id: state})
            except (OSError, ValueError, KeyError) as exc:
                return SubmissionDiscovery(successful=False, errors=[f"Local receipt unavailable: {exc}"])
        try:
            submitted = datetime.fromisoformat(submitted_at.replace("Z", "+00:00"))
            if submitted.tzinfo is None:
                submitted = submitted.replace(tzinfo=timezone.utc)
            # Slurm parses local wall time. Allow clock skew around the durable intent.
            start = (submitted - timedelta(minutes=5)).astimezone().strftime("%Y-%m-%dT%H:%M:%S")
            end = (datetime.now(timezone.utc) + timedelta(minutes=5)).astimezone().strftime("%Y-%m-%dT%H:%M:%S")
        except (ValueError, TypeError) as exc:
            return SubmissionDiscovery(successful=False, errors=[f"Invalid submission timestamp: {exc}"])
        scope = ["--user", context.get("user") or getpass.getuser()]
        for key, option in (("account", "--account"), ("partition", "--partition"), ("cluster", "--clusters")):
            if context.get(key):
                scope.extend([option, context[key]])
        commands = [
            ["squeue", "--noheader", "--format=%i|%128j|%64T|%128u|%128a", "--name", name, *scope],
            ["sacct", "-X", "-n", "-P", "--duplicates", "--format=JobIDRaw,JobName%128,State%64,User%128,Account%128,Cluster%128", "--name", name,
             "--starttime", start, "--endtime", end, *scope],
        ]
        discovery = SubmissionDiscovery()
        # Query accounting first so live queue state wins if the job changes during lookup.
        for command in reversed(commands):
            try:
                output = subprocess.run(command, capture_output=True, text=True, check=True, timeout=30).stdout
                for line in output.splitlines():
                    parts = line.strip().split("|")
                    if len(parts) < (6 if command[0] == "sacct" else 5):
                        if line.strip():
                            raise ValueError(f"Malformed {command[0]} allocation row")
                        continue
                    job_id, job_name, state = (part.strip() for part in parts[:3])
                    if job_name != name:
                        continue
                    if parts[3].strip() != scope[1]:
                        raise ValueError("Allocation user differs from saved submission scope")
                    if context.get("account") and parts[4].strip() != context["account"]:
                        raise ValueError("Allocation account differs from saved submission scope")
                    if command[0] == "sacct" and context.get("cluster") and parts[5].strip() not in context["cluster"].split(","):
                        raise ValueError("Allocation cluster differs from saved submission scope")
                    if not job_id or not state:
                        raise ValueError(f"Incomplete {command[0]} allocation row")
                    state = state.split()[0].rstrip("+")
                    if state not in ACTIVE_STATES | TERMINAL_STATES:
                        raise ValueError(f"Unknown scheduler state: {state}")
                    discovery.allocations[job_id] = state
            except (subprocess.SubprocessError, OSError, ValueError) as exc:
                discovery.successful = False
                discovery.errors.append(f"{command[0]}: {exc}")
        return discovery

    def finished_jobs(self, job_ids: list[str]) -> dict[str, str]:
        if not job_ids:
            return {}
        if self.cluster == "slurm":
            return self._finished_slurm_jobs(job_ids)
        return self._finished_local_jobs(job_ids)

    def _finished_slurm_jobs(self, job_ids: list[str]) -> dict[str, str]:
        # -X: one line per job (its allocation), not per step
        out = subprocess.run(
            ["sacct", "-X", "-n", "-P", "-o", "JobID,State%64", "-j", ",".join(job_ids)],
            capture_output=True, text=True, check=True, timeout=120,
        ).stdout

        states = {job_id: "UNKNOWN" for job_id in job_ids}
        for line in out.splitlines():
            job_id, _, state = line.partition("|")
            if job_id in states:
                states[job_id] = state.split()[0].rstrip("+") if state else "UNKNOWN"  # "CANCELLED by 1234"
        return {job_id: (state if state in TERMINAL_STATES else "UNKNOWN")
                for job_id, state in states.items() if state not in ACTIVE_STATES}

    def _finished_local_jobs(self, job_ids: list[str]) -> dict[str, str]:
        """Local executor (no SLURM, for testing): the job id is the job's pid."""
        finished = {}
        for job_id in job_ids:
            job = self.jobs.get(job_id)
            if job is not None:  # our child: os.kill would still see it until it's reaped
                if job.done():
                    finished[job_id] = "FAILED" if job.exception() is not None else "COMPLETED"
                continue
            try:
                os.kill(int(job_id), 0)
            except (ProcessLookupError, PermissionError):  # gone, or pid reused by another user
                finished[job_id] = "FINISHED"  # an earlier process's job: its exit status is lost
        return finished
