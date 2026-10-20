from dataclasses_json import dataclass_json
from dataclasses import dataclass
from typing import List, Optional, Any
import hashlib
import os
from os import path
from aggregation import AGG_NAME_TO_CLASS

JOB_MODES = ("per_task", "per_submission")

@dataclass_json
@dataclass
class SlurmBackendConfig:
    config: dict[str, Any]
    __performance_hash: Optional[str] = None # type: ignore

    def assert_valid(self) -> None:
        assert isinstance(self.config, dict), "config must be a dictionary"

    def performance_hash(self) -> str:
        # Create a hash based on the config dictionary for performance comparison
        if self.__performance_hash is not None:
            return self.__performance_hash
        
        hasher = hashlib.sha256()
        config_str = str(sorted(self.config.items())) if self.config else ''
        hasher.update(config_str.encode('utf-8'))
        self.__performance_hash = hasher.hexdigest()
        return self.__performance_hash

@dataclass_json
@dataclass
class AssignmentTaskConfig:
    name: str
    test_script_path: str
    slurm_backend: SlurmBackendConfig
    skip: bool = False
    blocking: bool = False
    reduction: Optional[str] = None
    submission_timeout: Optional[int] = None # seconds; the test script is killed after this long
    setup_script: Optional[str] = None # trusted Bash script sourced before each grading script
    __performance_hash: Optional[str] = None # type: ignore

    def assert_valid(self) -> None:
        assert isinstance(self.name, str) and self.name, "name must be a non-empty string"
        assert self.test_script_path, "test_script_path must be provided"
        assert os.path.exists(self.test_script_path), f"test_script_path {self.test_script_path} does not exist"
        if self.setup_script is not None:
            assert isinstance(self.setup_script, str) and self.setup_script, "setup_script must be a non-empty string or None"
            assert os.path.isfile(self.setup_script), f"setup_script {self.setup_script} must be an existing file"
        self.slurm_backend.assert_valid()
        assert isinstance(self.skip, bool), "skip must be a boolean"
        assert isinstance(self.blocking, bool), "blocking must be a boolean"
        assert self.reduction is None or isinstance(self.reduction, str), "reduction must be a string or None"
        if self.reduction is not None:
            assert self.reduction in AGG_NAME_TO_CLASS, f"reduction {self.reduction} is not supported"
        assert self.submission_timeout is None or (isinstance(self.submission_timeout, int) and self.submission_timeout > 0), "submission_timeout must be a positive integer or None"

    def performance_hash(self) -> str:
        # Create a hash based on relevant fields for performance comparison
        # If these fields change, we discard previous cached results
        if self.__performance_hash is not None:
            return self.__performance_hash
        
        hasher = hashlib.sha256()
        hasher.update(self.name.encode('utf-8'))
        hasher.update(self.test_script_path.encode('utf-8'))

        # insert also the contents of the test script to the hash
        with open(self.test_script_path, 'rb') as f:
            hasher.update(f.read())

        # hasher.update(str(self.skip).encode('utf-8'))
        # hasher.update(str(self.blocking).encode('utf-8'))

        # Include slurm_backend config in the hash
        hasher.update(self.slurm_backend.performance_hash().encode('utf-8'))
        # Include reduction in the hash
        hasher.update((self.reduction or "").encode('utf-8'))
        # A different timeout can change the results
        hasher.update(str(self.submission_timeout).encode('utf-8'))
        if self.setup_script is not None:
            hasher.update(b"\0setup_script\0")
            hasher.update(self.setup_script.encode('utf-8'))
            with open(self.setup_script, 'rb') as f:
                hasher.update(f.read())
        self.__performance_hash = hasher.hexdigest()
        return self.__performance_hash

@dataclass_json
@dataclass
class AssignmentConfig:
    name: str
    tasks: List[AssignmentTaskConfig]
    preserve_repo_files: bool = False


    def assert_valid(self) -> None:
        assert isinstance(self.name, str) and self.name, "name must be a non-empty string"
        assert isinstance(self.preserve_repo_files, bool), "preserve_repo_files must be a boolean"
        assert isinstance(self.tasks, list) and self.tasks, "tasks must be a non-empty list"
        for task in self.tasks:
            task.assert_valid()

        # assert no duplicate task names
        task_names = [task.name for task in self.tasks]
        assert len(task_names) == len(set(task_names)), f"Duplicate task names found in assignment {self.name}"

@dataclass_json
@dataclass
class GraderConfig:
    working_dir: str
    grades_file: str
    worker_url: str # base URL of the grader Cloudflare Worker
    worker_private_key: str # path to the ECDSA P-256 admin key used to sign worker requests
    sentry_dsn: Optional[str] = None
    max_submitted_jobs: int = -1 # at most this many grading jobs queued or running at once; -1 = no limit
    job_mode: str = "per_task" # "per_task": one job per task; "per_submission": one job per (student, task)
    wait_for_jobs: bool = True # repeat passes until every job is done; false = one pass, for runs started on a schedule
    worker_publication: bool = False # requires the Worker's run-based grading API
    worker_discovery: str = "submissions" # "updates" reduces HEAD lookups, with periodic full reconciliation
    worker_reconcile_seconds: int = 3600
    submission_grace_seconds: int = 600 # continuous successful absence before retrying an uncertain SLURM submission
    max_submission_recoveries: int = 3 # presumed-absent submissions per evaluation before stopping for reconciliation

    def assert_valid(self) -> None:
        assert isinstance(self.working_dir, str) and self.working_dir, "working_dir must be a non-empty string"
        assert path.isabs(self.working_dir), "working_dir must be an absolute path"
        assert path.exists(self.working_dir), f"working_dir {self.working_dir} does not exist"
        assert isinstance(self.grades_file, str) and self.grades_file, "grades_file must be a non-empty string"
        assert isinstance(self.worker_url, str) and self.worker_url.startswith(("https://", "http://")), "worker_url must be an http(s) URL"
        assert isinstance(self.worker_private_key, str) and self.worker_private_key, "worker_private_key must be a non-empty string"
        assert path.exists(self.worker_private_key), f"worker_private_key {self.worker_private_key} does not exist"
        assert isinstance(self.max_submitted_jobs, int) and (self.max_submitted_jobs == -1 or self.max_submitted_jobs >= 1), "max_submitted_jobs must be -1 (no limit) or a positive integer"
        assert self.job_mode in JOB_MODES, f"job_mode must be one of {JOB_MODES}"
        assert isinstance(self.wait_for_jobs, bool), "wait_for_jobs must be a boolean"
        assert isinstance(self.worker_publication, bool), "worker_publication must be a boolean"
        assert self.worker_discovery in {"submissions", "updates"}, "worker_discovery must be submissions or updates"
        assert self.worker_discovery != "updates" or self.worker_publication, "updates discovery requires worker_publication"
        assert type(self.worker_reconcile_seconds) is int and self.worker_reconcile_seconds > 0, "worker_reconcile_seconds must be a positive integer"
        assert type(self.submission_grace_seconds) is int and self.submission_grace_seconds > 0, "submission_grace_seconds must be a positive integer"
        assert type(self.max_submission_recoveries) is int and self.max_submission_recoveries > 0, "max_submission_recoveries must be a positive integer"


@dataclass_json
@dataclass
class PusherConfig:
    github_repo: str
    github_pat: str
    save_file: str
    push: bool = True

    def assert_valid(self) -> None:
        assert isinstance(self.github_repo, str) and self.github_repo, "github_repo must be a non-empty string"
        assert isinstance(self.github_pat, str) and self.github_pat, "github_pat must be a non-empty string"
        assert isinstance(self.save_file, str) and self.save_file, "save_file must be a non-empty string"
        assert isinstance(self.push, bool), "push must be a boolean"

@dataclass_json
@dataclass
class ProgramConfig:
    grader: GraderConfig
    assignments: List[AssignmentConfig]
    pusher: Optional[PusherConfig] = None

    def assert_valid(self) -> None:
        for assignment in self.assignments:
            assignment.assert_valid()

        # assert no duplicate assignment names
        assignment_names = [assignment.name for assignment in self.assignments]
        assert len(assignment_names) == len(set(assignment_names)), "Duplicate assignment names found"

        self.grader.assert_valid()

        if self.pusher:
            self.pusher.assert_valid()
