# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

Python >=3.14, managed with `uv`. There is no test suite or linter configured.

- `uv sync` — install dependencies
- `uv run main.py` — run the grader (reads `grader-config.yaml` from the cwd; copy from `grader-config-example.yaml`)
- `uv run python -m grader_worker.GraderWorkerApi <worker URL> <admin key file> [assignment]` — sanity-check the worker URL and admin key by printing submissions

`grader-config.yaml`, `*.json` (incl. `leaderboard.json`), `keys/`, `*.lock` and `test_scripts/*.sh` are gitignored. Config validation (`assert_valid`) requires `working_dir`, the `worker_private_key` file and each `test_script_path` to already exist.

## Architecture

The grader is meant to run on a SLURM cluster: `main.py` → `Grader.grade()` ([grader/grader.py](grader/grader.py)).

Flow:
1. For each assignment, `GraderWorkerAPI` ([grader_worker/](grader_worker/)) lists submissions from the grader Cloudflare Worker (`POST /submissions` with `assignment_name` = the assignment's `name`). Requests are signed with the ECDSA P-256 admin key in `worker_private_key` (raw `r||s` signature over `METHOD\npath\ntimestamp\nsha256(body)`; the worker allows 60 s of clock skew). The worker returns the normalized `repo_ssh_url` and the generated private deploy key.
2. In the main process, `git ls-remote HEAD` is run once per submission (with its deploy key) and compared with each task's `TaskCache` ([grader/task_cache.py](grader/task_cache.py), one per assignment + task name, stored in `<working_dir>/.cache/<assignment>_<task>_cache.json`). Tasks with no updated submissions get no job. The cluster caps the number of jobs, so there is exactly one job per task, never one per submission.
3. For each task with updates, `SlurmRunner` ([runners/SlurmRunner.py](runners/SlurmRunner.py), via `submitit`) submits `SubmissionGrader.grade_all` ([grader/submission_grader.py](grader/submission_grader.py), one instance per task) with only the updated submissions, using the task's `slurm_backend.config` as submitit parameters. `blocking` tasks are waited on immediately; otherwise all jobs are awaited with `wait_all()`.
4. Inside the job, only SLURM rank 0 works. Each repo is cloned over SSH with the student's deploy key (`deploy_key_env` in [grader/utils.py](grader/utils.py) writes it to a 0600 temp file and sets `GIT_SSH_COMMAND`), the task's test script is copied in and run in its own process group (the group is killed after the task's `submission_timeout`, giving status `timeout`), and the **last stdout line** is parsed as JSON (`{"passed", "total", "times"}`). Each result is written atomically to `<working_dir>/results/<assignment>/<task>/<student_id>.json` as soon as it's ready, and the clone is deleted right after unless `preserve_repo_files`; the job returns nothing. That directory is cleared before each job is submitted.
5. `_retrieve_results` reads those result files (even if the job failed, so a timeout or crash only loses the submission in progress) and merges them into the existing `grades_file` (keyed by assignment + student name, so students not re-graded are preserved), and updates caches. `grade()` then saves the grades file and the caches (in that order, each written atomically) and only then optionally pushes the leaderboard via `pusher/github_pusher.py` (the only place a GitHub token is used).

Things that span files:
- **Cache invalidation**: `AssignmentTaskConfig.performance_hash()` (task name, script path *and contents*, SLURM config, reduction, submission_timeout) is stored in each cache file; if it changes, the cache is dropped and everything is re-graded.
- **Aggregations**: `aggregation/` holds `ABAggregation` subclasses registered in `AGG_NAME_TO_CLASS` (`mean`, `median`). A task's optional `reduction` selects one; `GradeResult` ([grader/structs.py](grader/structs.py)) uses it to compute per-task `avg_runtime`, and serializes only the name. The job's result files don't carry it: `_retrieve_results` builds it from the task config. New aggregations must be added to `AGGS_LIST`.
- **Result shape**: `GradeResult` stores every field as a dict keyed by task name, so the leaderboard JSON is per-student with per-task sub-dicts (README's "Output Format" section is outdated in this respect).
- The job is a bound method of `SubmissionGrader`, which holds only data (task config, working dir, flags), so only that and the job's arguments (submissions with their deploy keys, results dir) land in `slurm_logs/*_submitted.pkl`, never `Grader` or its config (GitHub PAT). Don't submit bound methods of `Grader`, and don't give `SubmissionGrader` references to config or `Grader` state. `slurm_logs/` is chmod 0700. `SlurmRunner.collect_results` reads `job._sub_jobs[0]` to return only rank 0's result; the grader calls it only to detect a failed job.

Note: the old `gh/` (GitHub API) and `google_form/` (Google Forms) submission sources were removed in favor of `grader_worker/`.
