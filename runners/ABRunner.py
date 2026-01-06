from abc import ABC, abstractmethod
from config.configs import AssignmentTaskConfig

class ABRunner(ABC):
    @abstractmethod
    def run(self, grading_function: callable[[AssignmentTaskConfig, ...]], task: AssignmentTaskConfig, *args, **kwargs) -> int:
        raise NotImplementedError()
    
    @abstractmethod
    def wait_all(self) -> None:
        raise NotImplementedError()
    
    @abstractmethod
    def wait(self, jobid: int) -> None:
        raise NotImplementedError()

    @abstractmethod
    def collect_results(self, jobid: int) -> dict:
        raise NotImplementedError()