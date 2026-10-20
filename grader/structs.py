from typing import Any, NotRequired, TypedDict

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
    submission_id: NotRequired[str]
    run_id: NotRequired[str]
    submission_token: NotRequired[str]
