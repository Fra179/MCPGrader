from collections import defaultdict
import json
from config import ProgramConfig, AssignmentConfig, AssignmentTaskConfig
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
from dataclasses import dataclass
from logger import build_logger
import os

@dataclass
class GradeResult:
    name: str
    commit_hash: str
    _status: str
    error: str
    stdout: str
    runtimes: list[float]
    data: dict

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "commit_hash": self.commit_hash,
            "status": self.status,
            "error": self.error,
            "stdout": self.stdout,
            "avg_runtime": sum(self.runtimes) / len(self.runtimes) if self.runtimes else 0.0,
            "data": self.data
        }
    
    @property
    def status(self) -> str:
        return self._status
    
    @status.setter
    def status(self, value: str) -> None:
        if self._status != "error":
            self._status = value

class Grader:
    def __init__(self, config: ProgramConfig, pat: str, logger: Logger) -> None:
        self.config = config
        self.pat = pat
        self.classroom = GithubClassroomAPI(pat)
        self.wd = Path(config.grader.working_dir)
        self.log = logger
        self.job_ids: list[tuple] = []
        self.runner: ABRunner = self._get_runner()
        self.previous_grades: dict = {}

    def _grade_assignment(self, assignment_cfg: AssignmentConfig) -> None:
        if assignment_cfg.invite_link:
            assignment = self.classroom.get_assignment_by(By.INVITE_LINK, assignment_cfg.invite_link)
        elif assignment_cfg.slug:
            assignment = self.classroom.get_assignment_by(By.SLUG, assignment_cfg.slug)
        elif assignment_cfg.id:
            assignment = self.classroom.get_assignment_by(By.ID, assignment_cfg.id)
        else:
            raise GitHubException("No valid identifier provided for assignment.")

        submissions = self.classroom.get_submissions_for_assignment(assignment.id)


        for task in assignment_cfg.tasks:
            blocking = task.blocking

            if task.skip:
                self.log.info("Skipping grading for task: %s[%s]", assignment_cfg.name, task.name)
                self.job_ids.append((assignment_cfg, task, None))
                continue
            
            self.log.info("Launching grading job for task: %s[%s]", assignment_cfg.name, task.name)
            job_id = self.runner.run(self._grade_task, task, submissions)
            self.job_ids.append((assignment_cfg, task, job_id))

            if blocking:
                self.log.info("Waiting for blocking task %s[%s] to complete", assignment_cfg.name, task.name)
                self.runner.wait(job_id)

    def _grade_task(self, task: AssignmentTaskConfig, submissions: list[SubmissionInfo]) -> list[dict]:
        task_id = int(os.environ.get('SLURM_PROCID', 0))
        data = []
        logger = build_logger(name=f"grader.task.{task.name}", level=self.log.level)

        if task_id != 0:
            return []

        for submission in submissions:
            res = self._grade_submission(submission, task, logger)
            data.append(res)

        return data
           
    def _grade_submission(self, submission: SubmissionInfo, task: AssignmentTaskConfig, log: Logger) -> dict:
        log.info("Grading submission for %s[%s]", submission.repository.full_name, task.name)
        repo_dir = self.wd / (submission.repository.full_name.replace('/', '_') + f"_{task.name}")
        
        if repo_dir.exists():
            log.info("Cleaning up existing repository directory %s", repo_dir)
            rmtree(repo_dir)

        repo_url = submission.repository.html_url.replace("https://", f"https://{self.pat}@")
        log.debug("Downloading %s", repo_url)
        repo = Repo.clone_from(repo_url, repo_dir)

        commit_hash = repo.head.commit.hexsha[:7]
        log.debug("Cloned repository at commit %s", commit_hash)

        result = self._grade_task_submission(task, submission, commit_hash, repo_dir)
            
        return result
    
    def _grade_task_submission(self, task: AssignmentTaskConfig, submission: SubmissionInfo, commit_hash: str, repo_dir: Path) -> dict:
        # Use slurm to run the grading script
        # Copy the grading script to the repo directory

        grading_script_dest = repo_dir / Path(task.test_script_path).name
        copyfile(task.test_script_path, grading_script_dest)

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
            try:
                data = json.loads(last_line)
                runtimes = data.get("times", [])
                status = "graded"
            except json.JSONDecodeError:
                self.log.error("Failed to parse grading script output as JSON: %s", last_line)
                status = "error"
                error = "Failed to parse grading script output as JSON. The script may have crashed or produced invalid output.\nLast line: " + last_line +  "\nFull stderr:\n" + result.stderr

            self.log.info("Grading result: %s", data)
            self.log.info("Grading script output: %s", result.stdout)
        except subprocess.CalledProcessError as e:
            self.log.error("Grading script failed with error: %s", e.stderr)
            error = e.stderr
            stdout = e.stdout
            status = "error"

        runtime = sum(runtimes) / len(runtimes) if runtimes else 0.0
        self.log.info("Average runtime for %s [%s]: %.4f ms", submission.repository.full_name, task.name, runtime)

        return {"name": submission.pretty_users, "repo_dir": str(repo_dir), "commit_hash": commit_hash, "status": status, "error": error, "stdout": stdout, "runtimes": runtimes, "data": data}  # Placeholder grade
 
    def _get_result_defaultdict(self) -> dict:
        return defaultdict(
            lambda: defaultdict(
                lambda: GradeResult("", "", "graded", "", "", [], {})
                )
            )
                
    def _retrieve_results(self, existing_data: dict) -> dict:
        data: dict[str, dict[str, GradeResult]] = self._get_result_defaultdict()
        
        repos_to_cleanup = set()

        for assignment_cfg, task, jobid in self.job_ids:
            if jobid is None:
                self.log.info("Skipping result retrieval for skipped task: %s[%s]", assignment_cfg.name, task.name)    
                continue

            task_results = self.runner.collect_results(jobid)
            self.log.info("Collected results for %s[%s]", assignment_cfg.name, task.name)

            for result in task_results:
                name = result["name"]
                data[assignment_cfg.name][name].name = name
                data[assignment_cfg.name][name].commit_hash = result["commit_hash"]
                data[assignment_cfg.name][name].status = result["status"]  # This uses the property setter, see the implementation
                data[assignment_cfg.name][name].error += f"\n\n--- Stderr for task {task.name} ---\n" + result["error"]
                data[assignment_cfg.name][name].stdout += f"\n\n--- Stdout for task {task.name} ---\n" + result["stdout"]
                data[assignment_cfg.name][name].runtimes.extend(result["runtimes"])
                data[assignment_cfg.name][name].data[task.name] = result["data"]

            
                repo_dir = Path(result["repo_dir"])
                if not assignment_cfg.preserve_repo_files:
                    repos_to_cleanup.add(repo_dir)

        result = existing_data

        for assignment_name, students in data.items():
            result[assignment_name] = [student.to_dict() for student in students.values()]

        # Cleanup repositories
        for repo_dir in repos_to_cleanup:
            rmtree(repo_dir)
            self.log.debug("Deleted repository files for %s", repo_dir.name.replace('_', '/'))
        
        return result

    def _get_runner(self) -> ABRunner:
        logs_dir = Path(self.config.grader.logs_dir) / "slurm_logs"
        logs_dir.mkdir(parents=True, exist_ok=True)
        return SlurmRunner(logs_folder=str(logs_dir))

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
        data = self._load_grades_file()
        self.previous_grades = data
        self.jobs = []

        for assignment in self.config.assignments:
            self.log.info("Launching grading job for assignment: %s", assignment.name)
            self._grade_assignment(assignment)

        self.log.info("Waiting for all grading jobs to complete")
        self.runner.wait_all()

        results = self._retrieve_results(data)

        self._save_grades_file(results)