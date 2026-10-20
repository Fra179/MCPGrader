from dataclasses import dataclass, field


@dataclass
class SubmissionInfo:
    id: str
    assignment_name: str
    student_email: str
    student_id: int
    repo_url: str
    ssh_url: str  # repo_url normalized by the worker to git@host:owner/repo.git
    deploy_key: str = field(repr=False)
    last_push: str | None = None  # when the worker received the latest push to the default branch
    last_commit: str | None = None  # commit SHA of that push, forgeable by the student: only a hint
    update_id: int | None = None  # pending push version, never a grading execution identity
