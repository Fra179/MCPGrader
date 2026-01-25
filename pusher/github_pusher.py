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

    def push_results(self, local_file_path: str):
        with open(local_file_path, "r") as file:
            content = file.read()

        try:
            contents = self.repo.get_contents(self.config.save_file)
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