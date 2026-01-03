import json
from config import ProgramConfig, AssignmentConfig
from git import Repo
from gh import GithubClassroomAPI
from gh.filters import By
from gh.exceptions import GitHubException
from gh.structs import SubmissionInfo
from .exceptions import GraderException
from pathlib import Path
from os import mkdir
from shutil import rmtree
from logging import Logger
from runners import *
from shutil import copyfile
import subprocess

class Grader:
    def __init__(self, config: ProgramConfig, pat: str, logger: Logger) -> None:
        self.config = config
        self.pat = pat
        self.classroom = GithubClassroomAPI(pat)
        self.wd = Path(config.grader.working_dir)
        self.log = logger

    def _grade_assignment(self, assignment_cfg: AssignmentConfig) -> list[dict]:
        if assignment_cfg.invite_link:
            assignment = self.classroom.get_assignment_by(By.INVITE_LINK, assignment_cfg.invite_link)
        elif assignment_cfg.slug:
            assignment = self.classroom.get_assignment_by(By.SLUG, assignment_cfg.slug)
        elif assignment_cfg.id:
            assignment = self.classroom.get_assignment_by(By.ID, assignment_cfg.id)
        else:
            raise GitHubException("No valid identifier provided for assignment.")

        submissions = self.classroom.get_submissions_for_assignment(assignment.id)
        grades = []

        for submission in submissions:
            grade = self._grade_submission(submission, assignment_cfg)
            grades.append(grade)

        return grades

    def _grade_submission(self, submission: SubmissionInfo, assignment_cfg: AssignmentConfig) -> dict:
        self.log.info("Grading submission for %s", submission.repository.full_name)
        repo_dir = self.wd / submission.repository.full_name.replace('/', '_')
        if not repo_dir.exists():
            mkdir(repo_dir)

        repo_url = submission.repository.html_url.replace("https://", f"https://{self.pat}@")
        self.log.debug("Downloading %s", repo_url)
        repo = Repo.clone_from(repo_url, repo_dir)

        commit_hash = repo.head.commit.hexsha[:7]
        self.log.debug("Cloned repository at commit %s", commit_hash)

        # Use slurm to run the grading script
        # Copy the grading script to the repo directory
        grading_script_dest = repo_dir / Path(assignment_cfg.test_script_path).name
        copyfile(assignment_cfg.test_script_path, grading_script_dest)

        # Make the grading script executable
        grading_script_dest.chmod(0o755)

        self.log.debug("Copied grading script to %s", grading_script_dest)

        # Run inside the slurm environment, pipe stdout to a variable
        self.log.info("Running grading script")
        
        data = None
        error = ""
        status = ""
        stdout = ""
        runtimes = []
        
        try:
            result = subprocess.run([grading_script_dest], cwd=repo_dir, capture_output=True, text=True, check=True)
            result.stdout = result.stdout.strip()
            stdout = result.stdout
            last_line = result.stdout.split('\n')[-1]

            # {'passed': 12, 'total': 12, 'times': [442.44458, 664.421387, 886.576111, 354.137085, 442.864655, 663.164917, 884.586487, 354.348022, 443.62854, 664.1828, 885.255188, 354.565033]}
            data = json.loads(last_line)
            runtimes = data.get("times", [])
            status = "graded"

            self.log.info("Grading result: %s", data)
            self.log.info("Grading script output: %s", result.stdout)
        except subprocess.CalledProcessError as e:
            self.log.error("Grading script failed with error: %s", e.stderr)
            error = e.stderr
            stdout = e.stdout
            status = "error"
        
        # Cleanup repository files if not preserving
        if not assignment_cfg.preserve_repo_files:
            rmtree(repo_dir)
            self.log.info("Deleted repository files for %s", submission.repository.full_name)
        self.log.info("Finished grading %s", submission.repository.full_name)

        runtime = sum(runtimes) / len(runtimes) if runtimes else 0.0
        self.log.info("Average runtime for %s: %.4f ms", submission.repository.full_name, runtime)

        return {"name": submission.pretty_users, "commit_hash": commit_hash, "status": status, "error": error, "stdout": stdout, "avg_runtime": runtime, "data": data}  # Placeholder grade
        
    def _get_runner(self) -> ABRunner:
        return SlurmRunner()

    def _load_grades_file(self) -> dict:
        grades_file_path = Path(self.config.grader.grades_file)
        if grades_file_path.exists():
            with open(grades_file_path, 'r') as f:
                return json.load(f)
        return {}
    
    def _save_grades_file(self, data: dict) -> None:
        grades_file_path = Path(self.config.grader.grades_file)
        with open(grades_file_path, 'w') as f:
            json.dump(data, f, indent=4)

    def grade(self):
        runner = self._get_runner()
        job_ids = []
        data = self._load_grades_file()

        for assignment in self.config.assignments:
            blocking = assignment.blocking
            if assignment.skip:
                self.log.info("Skipping assignment: %s", assignment.name)
                job_ids.append(None)
                continue

            self.log.info("Launching grading job for assignment: %s (blocking=%s)", assignment.name, blocking)

            jobid = runner.run(self._grade_assignment, assignment)
            job_ids.append(jobid)

            if blocking:
                self.log.info("Waiting for blocking assignment: %s", assignment.name)
                runner.wait(jobid)

        for jobid, assignment in zip(job_ids, self.config.assignments):
            if assignment.skip:
                continue
            results = runner.collect_results(jobid)
            data[assignment.name] = results
            self._save_grades_file(data)

            self.log.info("Saved grades for assignment: %s", assignment.name)