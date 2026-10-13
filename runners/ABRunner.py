from abc import ABC, abstractmethod
from typing import Any, Callable
from config.configs import AssignmentTaskConfig

class ABRunner(ABC):
    @abstractmethod
    def run(self, grading_function: Callable[..., Any], task: AssignmentTaskConfig, *args, **kwargs) -> int:
        """Submit grading_function(*args, **kwargs) as a job and return its id; task only supplies its
        scheduling parameters. Submission may be deferred until wait/wait_all (e.g. by a job limit)."""
        raise NotImplementedError()
    
    @abstractmethod
    def wait_all(self) -> None:
        raise NotImplementedError()
    
    @abstractmethod
    def wait(self, jobid: int) -> None:
        raise NotImplementedError()

    @abstractmethod
    def collect_results(self, jobid: int) -> Any:
        """Return value of the job's function; raises if the job failed."""
        raise NotImplementedError()