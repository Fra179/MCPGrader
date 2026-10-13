from typing import Any, TypedDict

class TaskGradeResult(TypedDict):
    name: str
    commit_hash: str
    repo_url: str
    status: str
    error: str
    stdout: str
    runtimes: list[float]
    data: dict[str, Any] | None
