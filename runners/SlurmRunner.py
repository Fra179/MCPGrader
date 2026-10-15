from .ABRunner import ABRunner
from config.configs import AssignmentTaskConfig
import os
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

class SlurmRunner(ABRunner):
    """Submits jobs through submitit and reads their states back with sacct, so a later process can
    tell when an earlier one's jobs are done. Without SLURM, submitit runs jobs as local processes."""

    def __init__(self, logs_folder: str = "./logs"):
        super().__init__()
        self.logs_folder = logs_folder
        self.cluster = submitit.AutoExecutor(folder=logs_folder).cluster
        self.jobs: dict[str, Job] = {}  # submitted by this process

    def submit(self, grading_function: Callable[..., Any], task: AssignmentTaskConfig, *args, **kwargs) -> str:
        config = task.slurm_backend.config.copy()

        if not config.get("slurm_job_name"):
            config["slurm_job_name"] = f"grading_{task.name}"

        config["slurm_use_srun"] = False  # We are already running inside a SLURM job

        # create a new executor as not to carry over previous configs
        executor = submitit.AutoExecutor(folder=self.logs_folder, cluster=self.cluster)
        # task only supplies the SLURM parameters; the function gets just args/kwargs
        executor.update_parameters(**config)
        job = executor.submit(grading_function, *args, **kwargs)
        self.jobs[job.job_id] = job
        return job.job_id

    def finished_jobs(self, job_ids: list[str]) -> dict[str, str]:
        if not job_ids:
            return {}
        if self.cluster == "slurm":
            return self._finished_slurm_jobs(job_ids)
        return self._finished_local_jobs(job_ids)

    def _finished_slurm_jobs(self, job_ids: list[str]) -> dict[str, str]:
        # -X: one line per job (its allocation), not per step
        out = subprocess.run(
            ["sacct", "-X", "-n", "-P", "-o", "JobID,State", "-j", ",".join(job_ids)],
            capture_output=True, text=True, check=True, timeout=120,
        ).stdout

        states = {job_id: "UNKNOWN" for job_id in job_ids}
        for line in out.splitlines():
            job_id, _, state = line.partition("|")
            if job_id in states:
                states[job_id] = state.split()[0] if state else "UNKNOWN"  # "CANCELLED by 1234"
        return {job_id: state for job_id, state in states.items() if state not in ACTIVE_STATES}

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
