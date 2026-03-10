from dataclasses_json import dataclass_json
from dataclasses import dataclass
from typing import List, Optional, Any
import hashlib
import os
from os import path
from aggregation import AGG_NAME_TO_CLASS

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
    __performance_hash: Optional[str] = None # type: ignore

    def assert_valid(self) -> None:
        assert isinstance(self.name, str) and self.name, "name must be a non-empty string"
        assert self.test_script_path, "test_script_path must be provided"
        assert os.path.exists(self.test_script_path), f"test_script_path {self.test_script_path} does not exist"
        self.slurm_backend.assert_valid()
        assert isinstance(self.skip, bool), "skip must be a boolean"
        assert isinstance(self.blocking, bool), "blocking must be a boolean"
        assert self.reduction is None or isinstance(self.reduction, str), "reduction must be a string or None"
        if self.reduction is not None:
            assert self.reduction in AGG_NAME_TO_CLASS, f"reduction {self.reduction} is not supported"

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
        self.__performance_hash = hasher.hexdigest()
        return self.__performance_hash

    def __hash__(self) -> int:
        return hash(self.performance_hash())

@dataclass_json
@dataclass
class AssignmentConfig:
    name: str
    tasks: List[AssignmentTaskConfig]
    invite_link: Optional[str] = None
    slug: Optional[str] = None
    id: Optional[int] = None
    preserve_repo_files: bool = False


    def assert_valid(self) -> None:
        assert isinstance(self.name, str) and self.name, "name must be a non-empty string"
        assert any([self.invite_link, self.slug, self.id]), "at least one of invite_link, slug, or id must be provided"
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
    sentry_dsn: Optional[str] = None
    github_pat: Optional[str] = None

    def assert_valid(self) -> None:
        assert isinstance(self.working_dir, str) and self.working_dir, "working_dir must be a non-empty string"
        assert path.exists(self.working_dir), f"working_dir {self.working_dir} does not exist"
        assert isinstance(self.grades_file, str) and self.grades_file, "grades_file must be a non-empty string"

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