from .ABRunner import ABRunner
from config.configs import AssignmentConfig
import submitit
from submitit import Job

class SlurmRunner(ABRunner):
    def __init__(self):
        super().__init__()
        self.executor = submitit.AutoExecutor(folder="/tmp/slurm_jobs")
        self.jobs: list[Job] = []
        self.job_idx = 0

    def run(self, grading_function: callable[[AssignmentConfig]], assignment: AssignmentConfig) -> int:
        if not assignment.slurm_backend.config.get("slurm_job_name"):
            assignment.slurm_backend.config["slurm_job_name"] = f"grading_{assignment.name}"

        self.executor.update_parameters(**assignment.slurm_backend.config)
        job = self.executor.submit(grading_function, assignment)
        jobid = self.job_idx
        self.jobs.append(job)
        self.job_idx += 1
        return jobid
    
    def wait_all(self) -> None:
        for job in self.jobs:
            job.wait()

    def wait(self, jobid: int) -> None:
        job = self.jobs[jobid]
        job.wait()
    
    def collect_results(self, jobid: int) -> dict:
        job = self.jobs[jobid]
        return job.results()[0]