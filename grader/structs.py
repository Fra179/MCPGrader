from dataclasses import dataclass
from typing import Any, TypedDict
from aggregation import AGG_NAME_TO_CLASS
from aggregation.ABAggregation import ABAggregation

class TaskGradeResult(TypedDict):
    name: str
    repo_dir: str
    commit_hash: str
    repo_url: str
    status: str
    error: str
    stdout: str
    runtimes: list[float]
    data: dict[str, Any] | None
    reduction: ABAggregation

@dataclass
class GradeResult:
    name: str
    commit_hash: dict[str, str]
    status: dict[str, str]
    error: dict[str, str]
    stdout: dict[str, str]
    runtimes: dict[str, list[float]]
    data: dict[str, dict]
    reduction: dict[str, ABAggregation]

    def __avg_runtime(self, task_name: str) -> float:
        times = self.runtimes.get(task_name, [])
        agg_func = self.reduction.get(task_name)
        if agg_func:
            return agg_func.aggregate(times)
        elif not times:
            return 0.0
        return sum(times) / len(times)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "commit_hash": self.commit_hash,
            "tasks": list(self.status.keys()),
            "status": self.status,
            "error": self.error,
            "stdout": self.stdout,
            "runtimes": self.runtimes,
            "avg_runtime": {task: self.__avg_runtime(task) for task in self.runtimes},
            "data": self.data,
            "reduction": {task: agg.name() for task, agg in self.reduction.items()}
        }
    
    @staticmethod
    def from_dict(info: dict) -> "GradeResult":
        return GradeResult(
            name=info.get("name", ""),
            commit_hash=info.get("commit_hash", {}),
            status=info.get("status", {}),
            error=info.get("error", {}),
            stdout=info.get("stdout", {}),
            runtimes=info.get("runtimes", {}),
            data=info.get("data", {}),
            reduction={task: AGG_NAME_TO_CLASS[agg_name]() for task, agg_name in info.get("reduction", {}).items()}  
        )
    
    def update_from_dict(self, info: TaskGradeResult, task_name: str) -> None:
        self.name = info.get("name", self.name)
        self.commit_hash[task_name] = info.get("commit_hash", "unknown")
        self.status[task_name] = info.get("status", "error")
        self.error[task_name] = info.get("error", "Unknown error")
        self.stdout[task_name] = info.get("stdout", "")
        self.runtimes[task_name] = info.get("runtimes", [])
        data_val = info.get("data")
        self.data[task_name] = data_val if data_val is not None else {}
        self.reduction[task_name] = info.get("reduction", None)