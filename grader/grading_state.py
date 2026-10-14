import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TypedDict

from aggregation import AGG_NAME_TO_CLASS
from config import ProgramConfig, AssignmentTaskConfig
from grader_worker import SubmissionInfo
from .structs import TaskGradeResult
from .utils import write_json_atomic

STATE_VERSION = 1

# Key aliases, for readability only: a type checker treats them all as str
type AssignmentName = str
type StudentId = str  # the worker's student_id, as a string (JSON object keys)
type TaskName = str

class TaskRecord(TypedDict):
    """One student's latest result for one task, and what it was graded with."""
    commit_hash: str
    repo_url: str  # the ssh_url the commit was cloned from
    perf_hash: str  # AssignmentTaskConfig.performance_hash() at grading time
    graded_at: str  # ISO 8601, UTC
    status: str  # "graded", "error" or "timeout"
    error: str
    stdout: str
    runtimes: list[float]
    data: dict[str, Any]  # the test script's JSON output

type Records = dict[AssignmentName, dict[StudentId, dict[TaskName, TaskRecord]]]

class JobRecord(TypedDict):
    """A grading job submitted to SLURM and not yet seen finished."""
    job_id: str
    assignment: AssignmentName
    task: TaskName
    students: list[StudentId]  # whose submissions it grades
    perf_hash: str
    submitted_at: str  # ISO 8601, UTC

class AttemptRecord(TypedDict):
    """How many jobs were submitted for a submission without producing its result."""
    commit_hash: str | None  # None: git ls-remote failed
    perf_hash: str
    count: int

type Attempts = dict[AssignmentName, dict[StudentId, dict[TaskName, AttemptRecord]]]

class GradingState:
    """Everything the grader knows about past grading: one record per (assignment, student, task),
    holding the latest result and the commit and task configuration it was graded with, plus the
    grading jobs in flight, so a later run neither loses track of them nor grades their students again,
    and how many jobs each submission still waiting for a result has already had.

    It answers "does this submission need grading?" and produces the leaderboard, so the two can
    never disagree: a result and the commit it belongs to are saved together, in one atomic write.
    Only the main process may use it; jobs write result files, which the main process records here.

    TODO (no immediate need): store this in SQLite instead of a JSON file, keeping the interface.
    Only the main process should open it: the jobs keep writing result files. Use the default rollback
    journal, not WAL, whose shared memory isn't coherent across nodes on Lustre.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self.records: Records = {}
        self.jobs: list[JobRecord] = []
        self.attempts: Attempts = {}

        if path.exists():
            with open(path, 'r') as f:
                saved = json.load(f)
            if saved.get("version") != STATE_VERSION:
                raise ValueError(f"{path} has grading state version {saved.get('version')}, expected {STATE_VERSION}")
            self.records = saved["records"]
            self.jobs = saved.get("jobs", [])  # absent in files written before jobs were tracked
            self.attempts = saved.get("attempts", {})

    def needs_grading(self, assignment_name: AssignmentName, task: AssignmentTaskConfig, submission: SubmissionInfo, commit_hash: str) -> bool:
        """True unless this exact commit of this repository was graded with the task's current configuration."""
        record = self.records.get(assignment_name, {}).get(str(submission.student_id), {}).get(task.name)
        return (
            record is None
            or record["commit_hash"] != commit_hash
            or record["repo_url"] != submission.ssh_url  # the student switched repositories
            or record["perf_hash"] != task.performance_hash()  # test script or task config changed
        )

    def record(self, assignment_name: AssignmentName, task_name: TaskName, result: TaskGradeResult) -> None:
        """Store a job's result for one student, replacing their previous one for this task."""
        data = result.get("data")
        entry: TaskRecord = {
            "commit_hash": result["commit_hash"],
            "repo_url": result["repo_url"],
            "perf_hash": result["perf_hash"],
            "graded_at": datetime.now(timezone.utc).isoformat(),
            "status": result["status"],
            "error": result["error"],
            "stdout": result["stdout"],
            "runtimes": result["runtimes"],
            "data": data if data is not None else {},
        }
        self.records.setdefault(assignment_name, {}).setdefault(result["name"], {})[task_name] = entry
        student_attempts = self.attempts.get(assignment_name, {})
        if student_attempts.get(result["name"], {}).pop(task_name, None) is not None and not student_attempts[result["name"]]:
            del student_attempts[result["name"]]

    def attempt_count(self, assignment_name: AssignmentName, student_id: StudentId, task_name: TaskName, commit_hash: str | None, perf_hash: str) -> int:
        """Jobs already submitted for this commit and task configuration that didn't record its result."""
        attempt = self.attempts.get(assignment_name, {}).get(student_id, {}).get(task_name)
        if attempt is None or attempt["commit_hash"] != commit_hash or attempt["perf_hash"] != perf_hash:
            return 0
        return attempt["count"]

    def add_attempt(self, assignment_name: AssignmentName, student_id: StudentId, task_name: TaskName, commit_hash: str | None, perf_hash: str) -> None:
        count = self.attempt_count(assignment_name, student_id, task_name, commit_hash, perf_hash) + 1
        self.attempts.setdefault(assignment_name, {}).setdefault(student_id, {})[task_name] = {
            "commit_hash": commit_hash,
            "perf_hash": perf_hash,
            "count": count,
        }

    def add_job(self, job_id: str, assignment_name: AssignmentName, task_name: TaskName, students: list[StudentId], perf_hash: str) -> None:
        self.jobs.append({
            "job_id": job_id,
            "assignment": assignment_name,
            "task": task_name,
            "students": students,
            "perf_hash": perf_hash,
            "submitted_at": datetime.now(timezone.utc).isoformat(),
        })

    def remove_job(self, job_id: str) -> None:
        self.jobs = [job for job in self.jobs if job["job_id"] != job_id]

    def task_jobs(self, assignment_name: AssignmentName, task_name: TaskName) -> list[JobRecord]:
        return [job for job in self.jobs if job["assignment"] == assignment_name and job["task"] == task_name]

    def save(self) -> None:
        write_json_atomic(self.path, {"version": STATE_VERSION, "records": self.records, "jobs": self.jobs, "attempts": self.attempts}, indent=4)

    def export_leaderboard(self, config: ProgramConfig) -> dict[AssignmentName, list[dict[str, Any]]]:
        """The leaderboard JSON: per assignment, one entry per student with per-task sub-dicts.

        Each task's reduction is looked up in the current config (mean if unset, or if the task
        is no longer configured) and turns the reported times into avg_runtime.
        """
        # TODO: drop students no longer in the worker and tasks no longer in the config.
        reductions = {
            (assignment.name, task.name): task.reduction or "mean"
            for assignment in config.assignments for task in assignment.tasks
        }

        leaderboard: dict[AssignmentName, list[dict[str, Any]]] = {}
        for assignment_name, students in self.records.items():
            entries = []
            for student_id, tasks in students.items():
                reduction = {task: reductions.get((assignment_name, task), "mean") for task in tasks}
                entries.append({
                    "name": student_id,
                    "commit_hash": {task: r["commit_hash"] for task, r in tasks.items()},
                    "tasks": list(tasks),
                    "status": {task: r["status"] for task, r in tasks.items()},
                    "error": {task: r["error"] for task, r in tasks.items()},
                    "stdout": {task: r["stdout"] for task, r in tasks.items()},
                    "runtimes": {task: r["runtimes"] for task, r in tasks.items()},
                    "avg_runtime": {task: AGG_NAME_TO_CLASS[reduction[task]]().aggregate(r["runtimes"]) for task, r in tasks.items()},
                    "data": {task: r["data"] for task, r in tasks.items()},
                    "reduction": reduction,
                })
            leaderboard[assignment_name] = entries
        return leaderboard
