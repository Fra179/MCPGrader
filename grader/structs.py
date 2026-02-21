from dataclasses import dataclass
from typing import Any, TypedDict

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

@dataclass
class GradeResult:
    name: str
    commit_hash: dict[str, str]
    status: dict[str, str]
    error: dict[str, str]
    stdout: dict[str, str]
    runtimes: dict[str, list[float]]
    data: dict[str, dict]

    def __avg_runtime(self, task_name: str) -> float:
        times = self.runtimes.get(task_name, [])
        if not times:
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
            "data": self.data
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
            data=info.get("data", {})
        )
    
    def update_from_dict(self, info: TaskGradeResult, task_name: str) -> None:
        self.name = info.get("name", self.name)
        self.commit_hash[task_name] = info.get("commit_hash", "unknown")
        self.status[task_name] = info.get("status", "error")
        self.error[task_name] = info.get("error", "Unknown error")
        self.stdout[task_name] = info.get("stdout", "")
        self.runtimes[task_name] = info.get("runtimes", [])
        self.data[task_name] = info.get("data", {})

    def __add__(self, other: "GradeResult") -> "GradeResult":
        if self.name != other.name:
            raise ValueError("Cannot add GradeResults with different names")
        
        merged = GradeResult(
            name=self.name,
            commit_hash={**self.commit_hash, **other.commit_hash},
            status={**self.status, **other.status},
            error={**self.error, **other.error},
            stdout={**self.stdout, **other.stdout},
            runtimes={**self.runtimes, **other.runtimes},
            data={**self.data, **other.data}
        )
        return merged