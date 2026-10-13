import json
from pathlib import Path
from .utils import write_json_atomic

class TaskCache:
    """Last graded commit of each submission (by ssh_url) for one (assignment, task).

    Starts empty if the task's performance hash changed since it was saved, so a different
    test script or SLURM config makes every submission be regraded.
    """

    def __init__(self, path: Path, perf_hash: str) -> None:
        self.path = path
        self.perf_hash = perf_hash
        self.commits: dict[str, str] = {}

        if path.exists():
            with open(path, 'r') as f:
                saved = json.load(f)
            if saved.get("perf_hash") == perf_hash:
                self.commits = saved.get("cache", {})

    def is_graded(self, ssh_url: str, commit_hash: str) -> bool:
        return self.commits.get(ssh_url) == commit_hash

    def record(self, ssh_url: str, commit_hash: str) -> None:
        self.commits[ssh_url] = commit_hash

    def save(self) -> None:
        write_json_atomic(self.path, {"perf_hash": self.perf_hash, "cache": self.commits}, indent=4)
