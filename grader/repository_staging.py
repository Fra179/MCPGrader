"""Download pinned repositories in the coordinator; jobs only read local Git bundles."""

import hashlib
import os
from pathlib import Path
import tempfile

from git import Repo, exc

from grader_worker import SubmissionInfo
from .utils import deploy_key_env


class RepositoryStaging:
    def __init__(self, working_dir: Path):
        self.directory = working_dir / "repository_bundles"

    def stage(self, submission: SubmissionInfo, commit_hash: str) -> Path:
        identity = hashlib.sha256(f"{submission.ssh_url}\n{commit_hash}".encode()).hexdigest()
        bundle = self.directory / f"{identity}.bundle"
        if bundle.exists():
            return bundle

        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="download_", dir=self.directory) as temporary:
            root = Path(temporary)
            with deploy_key_env(submission.deploy_key) as env:
                with Repo.clone_from(submission.ssh_url, root / "repo", bare=True, env=env) as repo:
                    try:
                        repo.git.cat_file("-e", f"{commit_hash}^{{commit}}")
                    except exc.GitCommandError:
                        with repo.git.custom_environment(**env):
                            repo.git.fetch("origin", commit_hash)
                    # A named ref makes even a commit removed from the default branch bundleable.
                    repo.git.update_ref("refs/heads/grader-snapshot", commit_hash)
                    temporary_bundle = root / "snapshot.bundle"
                    repo.git.bundle("create", str(temporary_bundle), "refs/heads/grader-snapshot")
            temporary_bundle.chmod(0o600)
            with temporary_bundle.open("rb") as stream:
                os.fsync(stream.fileno())
            os.replace(temporary_bundle, bundle)
            directory_fd = os.open(self.directory, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        return bundle

    def cleanup(self, retained: set[str]) -> None:
        """Called under the run lock, after newly submitted jobs have been saved."""
        for bundle in self.directory.glob("*.bundle"):
            if str(bundle) not in retained:
                bundle.unlink(missing_ok=True)
