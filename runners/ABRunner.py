from abc import ABC, abstractmethod
from typing import Any, Callable
from dataclasses import dataclass, field
from config.configs import AssignmentTaskConfig

class DefiniteSubmissionError(RuntimeError):
    """Submission failed before the scheduler could accept a job."""


@dataclass
class SubmissionDiscovery:
    allocations: dict[str, str] = field(default_factory=dict)
    successful: bool = True
    errors: list[str] = field(default_factory=list)


class ABRunner(ABC):
    def submission_context(self, task: AssignmentTaskConfig) -> dict:
        """Serializable scheduler scope used to recover a submission after a restart."""
        return {}

    def discover_submission(self, submission_token: str, submitted_at: str,
                            context: dict) -> SubmissionDiscovery:
        """Find allocations by durable token; absence is authoritative only if successful."""
        return SubmissionDiscovery(successful=False, errors=["Recovery is unsupported"])

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
