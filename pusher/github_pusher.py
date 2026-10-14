from typing import Optional
from dataclasses_json import dataclass_json
from config.configs import PusherConfig
from github import Github

class GithubPusher:
    def __init__(self, config: PusherConfig, wd: Optional[str] = None):
        self.config = config
        self.wd = wd
        self.repo = self._get_repo()

    def _get_repo(self):
        g = Github(self.config.github_pat)
        return g.get_repo(self.config.github_repo)

    def push_results(self, local_file_path: str) -> Optional[str]:
        """Commit the file to the repository; returns the commit sha, or None if it's already there as is."""
        with open(local_file_path, "r") as file:
            content = file.read()

        try:
            contents = self.repo.get_contents(self.config.save_file)
            try:
                unchanged = contents.decoded_content.decode() == content
            except Exception:  # over 1 MB the API doesn't return the content: just push
                unchanged = False
            if unchanged:
                return None
            result = self.repo.update_file(
                contents.path,
                "Update leaderboard result",
                content,
                contents.sha,
            )
        except Exception:
            result = self.repo.create_file(
                self.config.save_file,
                "Create leaderboard result file",
                content,
            )

        return result["commit"].sha