from dataclasses import dataclass
from dataclasses_json import dataclass_json
from typing import Optional, List

@dataclass_json
@dataclass
class ClassroomInfo:
    id: int
    name: str
    archived: bool
    url: str


# Dataclass per rappresentare un Assignment (senza il campo "classroom")
@dataclass_json
@dataclass
class AssignmentInfo:
    id: int
    public_repo: bool
    title: str
    type: str
    invite_link: str
    invitations_enabled: bool
    slug: str
    students_are_repo_admins: bool
    feedback_pull_requests_enabled: bool
    max_teams: Optional[int]
    max_members: Optional[int]
    editor: Optional[str]
    accepted: int
    submissions: int
    passing: int
    language: Optional[str]
    deadline: Optional[str]


@dataclass_json
@dataclass
class StudentInfo:
    id: int
    login: str
    name: Optional[str]
    avatar_url: str
    html_url: str


@dataclass_json
@dataclass
class RepositoryInfo:
    id: int
    name: str
    full_name: str
    html_url: str
    node_id: str
    private: bool
    default_branch: str


# Rappresenta una Submission, referenzia AssignmentInfo e ignora il campo 'classroom' in input
@dataclass_json
@dataclass
class SubmissionInfo:
    id: int
    submitted: bool
    passing: bool
    commit_count: int
    grade: Optional[str]
    students: List[StudentInfo]
    assignment: AssignmentInfo
    repository: RepositoryInfo

    @property
    def pretty_users(self) -> str:
        return ", ".join([student.login for student in self.students])