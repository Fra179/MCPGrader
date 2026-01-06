from .ABRunner import ABRunner
from config.configs import AssignmentTaskConfig
import submitit
from submitit import Job
from typing import Callable

class SlurmRunner(ABRunner):
    def __init__(self, logs_folder: str = "./logs"):
        super().__init__()
        self.executor = submitit.AutoExecutor(folder=logs_folder)
        self.jobs: list[Job] = []
        self.job_idx = 0

    def run(self, grading_function: Callable[[AssignmentTaskConfig, ...]], task: AssignmentTaskConfig, *args, **kwargs) -> int:
        if not task.slurm_backend.config.get("slurm_job_name"):
            task.slurm_backend.config["slurm_job_name"] = f"grading_{task.name}"

        self.executor.update_parameters(**task.slurm_backend.config)
        job: Job = self.executor.submit(grading_function, *[task] + list(args), **kwargs)
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