from .ABRunner import ABRunner
from config.configs import AssignmentTaskConfig
import logging
import time
from collections import deque
from dataclasses import dataclass
import submitit
from submitit import Job
from typing import Any, Callable

@dataclass
class _QueuedJob:
    jobid: int
    function: Callable[..., Any]
    parameters: dict[str, Any]  # submitit parameters, applied when the job is actually submitted
    args: tuple
    kwargs: dict[str, Any]

class SlurmRunner(ABRunner):
    """Submits jobs through submitit, keeping at most max_submitted_jobs queued or running at once
    (-1 = no limit). Jobs over the limit wait in a local queue and are submitted by wait/wait_all
    as earlier jobs finish, so the cluster's per-user job limit is never hit."""

    def __init__(self, logs_folder: str = "./logs", max_submitted_jobs: int = -1, poll_interval: float = 30.0):
        super().__init__()
        self.executor = submitit.AutoExecutor(folder=logs_folder)
        self.max_submitted_jobs = max_submitted_jobs
        self.poll_interval = poll_interval
        self.jobs: list[Job | None] = []  # by job id; None until submitted
        self.queue: deque[_QueuedJob] = deque()
        self.active: set[int] = set()  # submitted and not finished yet
        self.submit_errors: dict[int, Exception] = {}
        self.log = logging.getLogger("grader.runner")

    def run(self, grading_function: Callable[..., Any], task: AssignmentTaskConfig, *args, **kwargs) -> int:
        config = task.slurm_backend.config.copy()

        if not config.get("slurm_job_name"):
            config["slurm_job_name"] = f"grading_{task.name}"

        config["slurm_use_srun"] = False  # We are already running inside a SLURM job

        # task only supplies the SLURM parameters; the function gets just args/kwargs
        jobid = len(self.jobs)
        self.jobs.append(None)
        self.queue.append(_QueuedJob(jobid, grading_function, config, args, kwargs))
        self._submit_queued()
        return jobid

    def _submit_queued(self) -> None:
        self.active = {jobid for jobid in self.active if not self.jobs[jobid].done()}

        while self.queue and (self.max_submitted_jobs == -1 or len(self.active) < self.max_submitted_jobs):
            queued = self.queue.popleft()
            try:
                self.executor.update_parameters(**queued.parameters)
                self.jobs[queued.jobid] = self.executor.submit(queued.function, *queued.args, **queued.kwargs)
                self.active.add(queued.jobid)
            except Exception as e:
                # e.g. sbatch refused it; reported by collect_results, retried on the next run
                self.log.error("Failed to submit job %d: %s", queued.jobid, str(e))
                self.submit_errors[queued.jobid] = e

        if self.queue:
            self.log.debug("%d jobs submitted, %d queued locally", len(self.active), len(self.queue))

    def _finished(self, jobid: int) -> bool:
        job = self.jobs[jobid]
        return jobid in self.submit_errors or (job is not None and job.done())

    def wait_all(self) -> None:
        while True:
            self._submit_queued()
            if not self.queue and all(self._finished(jobid) for jobid in range(len(self.jobs))):
                return
            time.sleep(self.poll_interval)

    def wait(self, jobid: int) -> None:
        while True:
            self._submit_queued()
            if self._finished(jobid):
                return
            time.sleep(self.poll_interval)

    def collect_results(self, jobid: int) -> Any:
        if jobid in self.submit_errors:
            raise RuntimeError(f"job was never submitted: {self.submit_errors[jobid]}")

        job = self.jobs[jobid]
        if job is None:
            raise RuntimeError("job was never submitted")

        # You shouldn't access private members of a class like this
        # but I need to "hack" the library in such a way that id doesn't
        # parse jobs results past rank #0
        sub_job = job._sub_jobs[0] if job._sub_jobs else job

        return sub_job.results()[0] # Only the first rank executes the scripts
