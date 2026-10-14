from abc import ABC, abstractmethod
from typing import Any, Callable
from config.configs import AssignmentTaskConfig

class ABRunner(ABC):
    @abstractmethod
    def submit(self, grading_function: Callable[..., Any], task: AssignmentTaskConfig, *args, **kwargs) -> str:
        """Submit grading_function(*args, **kwargs) as a job and return its id; task only supplies its
        scheduling parameters. Raises if the job couldn't be submitted."""
        raise NotImplementedError()

    @abstractmethod
    def finished_jobs(self, job_ids: list[str]) -> dict[str, str]:
        """Final state of each of these jobs that has finished ("COMPLETED", "FAILED", ...; "FINISHED" if
        the exit status is unknown; "UNKNOWN" if the scheduler doesn't know the job, which can briefly
        happen right after submitting it). Jobs still queued or running are left out. Works for jobs
        submitted by an earlier process too. Raises if the states can't be retrieved."""
        raise NotImplementedError()
