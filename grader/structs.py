from typing import Any, TypedDict

class TaskGradeResult(TypedDict):
    name: str
    commit_hash: str
    repo_url: str
    perf_hash: str  # AssignmentTaskConfig.performance_hash() when the job was submitted
    status: str
    error: str
    stdout: str
    runtimes: list[float]
    data: dict[str, Any] | None
