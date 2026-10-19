# MCPGrader

An automated grading system that reads student submissions from the [grader worker](#grader-worker-setup) (a Cloudflare Worker), clones each student's repository with the deploy key the worker issued for it, runs test scripts, collects performance metrics, and generates a leaderboard with results.

## Table of Contents

- [Overview](#overview)
- [Features](#features)
- [Installation](#installation)
- [Configuration](#configuration)
  - [Step 1: Copy the Example Configuration](#step-1-copy-the-example-configuration)
  - [Step 2: Configure the Grader Section](#step-2-configure-the-grader-section)
  - [Step 3: Configure the Pusher Section](#step-3-configure-the-pusher-section)
  - [Step 4: Configure Assignments](#step-4-configure-assignments)
  - [Step 5: Configure SLURM Backend for Tasks](#step-5-configure-slurm-backend-for-tasks)
- [Grader Worker Setup](#grader-worker-setup)
  - [Admin Key](#admin-key)
  - [What Students Submit](#what-students-submit)
- [Running the Grader](#running-the-grader)
- [Setting Up as a Recurrent Task](#setting-up-as-a-recurrent-task)
- [Test Script Format](#test-script-format)
- [Output Format](#output-format)

## Overview

This grading system automates the process of:
1. Reading student submissions (student ID, repository URL, deploy key) from the grader worker
2. Cloning student repositories over SSH with each student's deploy key
3. Running test scripts with configurable SLURM resources
4. Collecting results including pass/fail status and performance metrics
5. Generating a JSON leaderboard with all results

## Features

- **Worker Submissions**: Reads submissions from the grader worker's admin API, with signed requests
- **Per-Student Deploy Keys**: Clones private repositories without a GitHub token, using the read-only deploy key the worker generated for each repository
- **SLURM Support**: Execute test scripts with configurable SLURM resources (CPU, memory, GPUs, etc.)
- **Performance Metrics**: Collects runtime statistics from test scripts
- **Incremental Grading**: Only re-grades repositories whose latest commit changed since the last run
- **Blocking Tasks**: Hold back an assignment's later tasks until a task is fully graded
- **Restartable Runs**: A run can be killed at any point; the next one picks up the jobs and results it left
- **Repository Cleanup**: Option to preserve or delete cloned repositories
- **Leaderboard Generation**: JSON output with all results and metrics
- **GitHub Integration**: Push leaderboard results directly to a GitHub repository

## Installation

1. Clone this repository
2. Install dependencies using `uv` or `pip`:
   ```bash
   uv sync
   # or
   pip install -r requirements.txt
   ```

## Configuration

### Step 1: Copy the Example Configuration

Create your configuration file by copying the example:

```bash
cp grader-config-example.yaml grader-config.yaml
```

### Step 2: Configure the Grader Section

Edit the `grader` section in `grader-config.yaml`:

```yaml
grader:
  working_dir: "/tmp/grader" # must be an absolute path and already exist
  grades_file: "leaderboard.json"
  worker_url: "https://grader-api.example.com"
  worker_private_key: "keys/grader.key"
  sentry_dsn: ""
  max_submitted_jobs: -1
  job_mode: "per_task"
  wait_for_jobs: true
```

**Field Explanations:**

- **`working_dir`**: Absolute path to the directory where student repositories will be cloned. This directory must exist before running the grader; relative paths such as `./work` are rejected.
- **`grades_file`**: Path to the output JSON file containing all grading results and the leaderboard. It's rewritten on every run from `<working_dir>/grading_state.json`, where the grader keeps every student's latest result together with the commit and task configuration it was graded with. Don't edit the grades file by hand: changes are overwritten. Deleting `grading_state.json` makes the next run regrade everyone.
- **`worker_url`** (required): Base URL of the grader worker.
- **`worker_private_key`** (required): Path to the ECDSA P-256 admin private key (PEM) used to sign requests to the worker. This file must exist before running the grader. See [Admin Key](#admin-key).
- **`sentry_dsn`**: (Optional) Sentry DSN for error tracking and monitoring. Failed HEAD lookups (after three attempts) and repository downloads generate an explicit error event, once per submission and download stage per run, with assignment, student ID, repository URL and error details. Deploy keys and exception locals are not attached. Leave empty if not using Sentry.
- **`max_submitted_jobs`** (default: `-1`, no limit): At most this many grading jobs are queued or running on the cluster at once, across all tasks and assignments. Extra jobs are submitted by later passes as earlier ones finish (by the next run, with `wait_for_jobs: false`), so the cluster's per-user job limit isn't hit. Jobs from earlier runs that are still in flight count too. It only counts the grader's own jobs: if the same user submits other jobs, leave some margin. A job the cluster still refuses is logged, and its students are retried on the next run.
- **`job_mode`** (default: `"per_task"`): `"per_task"` grades all of a task's updated submissions one after another in a single job; `"per_submission"` launches one job per (student, task) for maximum parallelism. With `"per_submission"`, a task's `timeout_min` must cover a single student instead of all of them, and you'll usually want `max_submitted_jobs` set.
- **`wait_for_jobs`** (default: `true`): The grader works in passes: each pass records the results written so far, forgets jobs that have finished, and submits jobs for submissions that need grading. With `true`, a run repeats passes every 30 seconds until no job is in flight and nothing is left to submit. With `false`, a run makes a single pass and exits without waiting for its jobs: the next run records their results. Either way, the leaderboard is written (and pushed) once, when the run ends. Use `false` when the grader is started on a schedule, e.g. every 10 minutes.

### Step 3: Configure the Pusher Section

The `pusher` section is optional and allows you to automatically push the generated leaderboard to a GitHub repository:

```yaml
pusher:
  github_repo: "user/MCPLeaderboard"
  github_pat: "pat_example_1234567890"
  save_file: "data/leaderboard.json"
  push: true
```

**Field Explanations:**

- **`github_repo`**: The target GitHub repository in the format `owner/repo` where the leaderboard will be pushed (e.g., `"myorg/leaderboard"`).
- **`github_pat`**: GitHub Personal Access Token with write access to the target repository. The pusher is the only part of the grader that uses a GitHub token.
- **`save_file`**: Path within the target repository where the leaderboard JSON file will be saved (e.g., `"data/leaderboard.json"`).
- **`push`**: Whether to enable pushing to GitHub (default: `true`). Set to `true` to automatically commit and push the leaderboard after grading completes.

**Example Workflow:**
When `push: true`, the pusher will:
1. Clone the target GitHub repository
2. Write the leaderboard data to the specified `save_file`
3. Commit the changes with an appropriate commit message
4. Push the changes to the repository

This is useful for maintaining a public leaderboard that updates automatically with each grading run.

### Step 4: Configure Assignments

Add assignments to the `assignments` list. Each assignment can have multiple tasks that run different test scripts with different configurations:

```yaml
assignments:
  - name: "vector-sum"
    preserve_repo_files: false

    tasks:
      - name: "test-cpu"
        skip: false
        blocking: true
        test_script_path: "./test_scripts/test_vectorsum_cpu.sh"

        slurm_backend:
          config:
            slurm_partition: "short"
            timeout_min: 60
            mem_gb: 4
            nodes: 1
            tasks_per_node: 1
            cpus_per_task: 2
            gpus_per_node: 0
            
      - name: "test-gpu"
        skip: false
        blocking: true
        test_script_path: "./test_scripts/test_vectorsum_gpu.sh"
        reduction: "median"

        slurm_backend:
          config:
            slurm_partition: "gpu"
            timeout_min: 60
            mem_gb: 8
            nodes: 1
            tasks_per_node: 1
            cpus_per_task: 4
            gpus_per_node: 1
```

**Assignment-Level Field Explanations:**

- **`name`** (required): A unique identifier for the assignment. Used as the key in the output JSON, and as the `assignment_name` whose submissions are fetched from the worker, so it must match exactly what students submit.

- **`preserve_repo_files`** (default: `false`): If `true`, cloned repositories will not be deleted after grading. Useful for debugging.

- **`tasks`** (required): A list of tasks to run for this assignment. Each task represents a different test or evaluation.

**Task-Level Field Explanations:**

- **`name`** (required): A unique identifier for the task within the assignment. Used to identify the task in logs and output.

- **`skip`** (default: `false`): If `true`, this task will be skipped during grading.

- **`blocking`** (default: `false`): If `true`, the assignment's later tasks aren't submitted while this task still has jobs in flight or jobs waiting for `max_submitted_jobs` (or jobs the cluster refused); a later pass or run submits them once those are done. Useful when tasks shouldn't compete for the cluster or when later tasks depend on earlier ones.

- **`test_script_path`** (required): Absolute or relative path to the test script that will be executed in each student's repository for this task.
- **`setup_script`** (default: unset): Path to a trusted Bash script sourced before each submission's grading script. Relative paths are resolved from the coordinator's working directory; the original file must be accessible at that absolute path on compute nodes. It does not need executable permissions and is not copied into the student's repository. With setup enabled, a Bash login shell initializes the module environment, sources the setup with `errexit` enabled, then executes the grading script in the same shell, preserving exported variables and supporting Python grading scripts. Setup runs in the clone directory. A failing setup stops grading and produces an error result; `submission_timeout` covers setup and grading together. Adding/removing setup, changing its path, or editing its contents triggers regrading on the next run. Tasks without setup keep their existing launch behavior.

- **`submission_timeout`** (default: no limit): Seconds a single student's test script may run. On timeout the script's whole process group (including `make`, `srun` and the student's program) gets SIGTERM, then SIGKILL 5 s later, and the result is recorded with status `timeout`. With `job_mode: "per_task"` all of a task's submissions are graded one after another in a single SLURM job, so `timeout_min` should cover the number of updated submissions times this value; with `"per_submission"` it only needs to cover one.

- **`reduction`** (default: `"mean"`): How the list of times a test script reports (`"times"` in its JSON output) becomes the task's `avg_runtime` in the leaderboard: `"mean"` or `"median"`. The median is less sensitive to a single slow run. It only affects `avg_runtime`. Changing it regrades every submission of the task, like any change to the task's configuration.

>### [!] Important [!]
>When writing your test script don't forget to launch the student's script with either `srun`, `mpirun` or a similar command that will also schedule the process on the cluster!

For example, to load MPI on Leonardo, create `test_scripts/setup_mpi.sh`:

```bash
module load gcc/12.2.0 || return 2
module load openmpi/4.1.6--gcc--12.2.0-cuda-12.2 || return 2
```

Then add this to each MPI task alongside `test_script_path`:

```yaml
setup_script: "./test_scripts/setup_mpi.sh"
```

Explicitly export ordinary variables you want the grading script to inherit (for example, `export OMP_NUM_THREADS=4`). The grader's existing exit-code policy also applies to setup errors: they are cached until the commit or task configuration changes.

### Step 5: Configure SLURM Backend for Tasks

The `slurm_backend.config` section within each task accepts parameters that are passed directly to **submitit**, which manages SLURM job submissions. Each task can have different SLURM resource requirements.

```yaml
slurm_backend:
  config:
    slurm_partition: "short"      # SLURM partition name
    timeout_min: 120              # Maximum runtime in minutes
    mem_gb: 8                     # Memory allocation in GB
    nodes: 1                      # Number of nodes
    tasks_per_node: 1             # Tasks per node
    cpus_per_task: 8              # CPUs per task
    gpus_per_node: 0              # GPUs per node
```

**Common Parameters** (from submitit):

- **`slurm_partition`**: SLURM partition/queue name
- **`timeout_min`**: Job timeout in minutes
- **`mem_gb`**: Memory allocation per node in gigabytes
- **`nodes`**: Number of compute nodes
- **`tasks_per_node`**: Number of MPI tasks per node
- **`cpus_per_task`**: Number of CPU cores per task
- **`gpus_per_node`**: Number of GPUs per node
- **`slurm_account`**: SLURM account to charge (if required)
- **`slurm_qos`**: Quality of Service specification

For a complete list of available parameters, refer to the [submitit documentation](https://github.com/facebookincubator/submitit).

## Grader Worker Setup

Students submit their repositories to the grader worker (`grader-worker`), a Cloudflare Worker that stores each submission and the deploy key it generated for it. Deploy the worker as described in its README.

### Admin Key

The grader lists submissions with `POST /submissions`, an admin endpoint whose requests must be signed with an ECDSA P-256 key authorized in the worker's `ADMIN_PUBLIC_KEYS`. In the worker repository:

```bash
scripts/admin-keys.sh new grader-server   # keys/grader-server.key (private) + keys/grader-server.pub
scripts/admin-keys.sh push                # authorize it on the deployed worker
```

Copy `keys/grader-server.key` to the grader machine and point `grader.worker_private_key` at it. Keep it secret: it gives access to every student's deploy key. `keys/` is listed in `.gitignore`.

The worker rejects requests whose timestamp is more than 60 seconds off its clock, so keep the grader machine's clock in sync.

To check that the URL and the key work, print an assignment's submissions:

```bash
uv run python -m grader_worker.GraderWorkerApi https://grader-api.example.com keys/grader-server.key vector-sum
```

### What Students Submit

Students `POST /submit` to the worker with the assignment name, repository URL, student ID and email. The worker answers with the public half of a freshly generated deploy key, which the student adds under **Settings → Deploy keys → Add deploy key** with **Allow write access** unchecked.

Because the key is read-only and only works for that one repository, the grader can clone private repositories without a GitHub token. If a repository has no commits yet, or its deploy key hasn't been added, it is skipped until it does.

## Running the Grader

Once configured, run the grader:

```bash
uv run main.py
# or
python main.py
```

The grader works in passes (see `wait_for_jobs`). In each pass it will:
1. Forget the grading jobs that have finished, and record every result file jobs have written so far in `<working_dir>/grading_state.json`, including those of jobs started by an earlier run
2. For each assignment (unless skipped), submit grading jobs, up to `max_submitted_jobs` in flight:
   - Read student submissions for the assignment from the grader worker
   - Skip repositories whose latest commit was already graded with the task's current configuration, or that a job in flight is grading
   - Download each required pinned repository in the coordinator using its deploy key, and atomically create a Git bundle in `<working_dir>/repository_bundles/`
   - Submit jobs with local bundle paths and copies of the submissions with their deploy keys removed
3. Inside each grading job (no internet access required):
   - Clone the local bundle and explicitly check out the pinned commit
   - Copy the test script to the repository
   - Execute the test script
   - Parse the output for test results and performance metrics
   - Clean up (unless `preserve_repo_files` is true)

When the run ends, it writes the leaderboard to the `grades_file` and pushes it (the push is skipped if the leaderboard didn't change).

Submissions and their latest commits are snapshotted once per run. Jobs explicitly check out the snapshotted commit, so every task grades that commit even if the student pushes before the job starts; later commits are picked up by the next run. Failed HEAD lookups are retried up to three times before submission. Submissions whose HEAD still cannot be resolved are skipped until a later pass or run; successful lookups stay fixed for the run. If the pinned commit is missing from the coordinator's download, the coordinator tries to fetch it explicitly before creating the bundle. Download/fetch failures are logged and reported to Sentry when configured; the affected repository/commit is skipped for the rest of the run and retried on the next run. These failures submit no grading jobs, consume no grading attempts, and leave existing grades unchanged. Other submissions can still be graded; a failed download for a blocking task holds back later tasks in that assignment.

Bundles are identified by repository URL and commit, shared across tasks, and retained while any job in `grading_state.json` references them. Each pass removes unreferenced bundles after submitting new work, so queued jobs survive coordinator restarts. The coordinator and jobs must see `working_dir` at the same absolute path. Jobs submitted before this change keep their legacy remote-cloning behavior, and their old submitit pickles still contain deploy keys.

On Leonardo, run the coordinator on `lrd_all_serial` (GitHub HTTPS and SSH were verified there); DCGP and Booster jobs use the shared bundles. The worker endpoint and scheduler commands must also be reachable from the coordinator. Git bundles contain committed Git files and history; Git LFS objects and submodule repositories are not downloaded automatically.

The jobs in flight are kept in `grading_state.json`, so a run killed at any point loses nothing: the next one waits for those jobs and records their results instead of grading the same students again.

Only one run at a time works on a `working_dir`: a run holds a lock on `<working_dir>/grader.lock` until it ends, and a run started meanwhile logs who holds it and exits, so a schedule can start the grader as often as it likes. The lock is released when the run ends or dies, even with `kill -9` or at a SLURM time limit. At startup, `main.py` checks that locking works in `working_dir` and, on Lustre or NFS, that locks are shared across nodes; it refuses to run otherwise.

A submission whose grading jobs end 3 times without producing its result (the job crashed, was killed or hit `timeout_min`, or the repository couldn't be cloned) gets an `error` result instead of another job. A new commit or a change to the task tries again.

### Recovering from earlier compute-node download failures

The staging change does not invalidate existing grades. If a previous run exhausted its attempt cap because compute nodes could not download repositories, reset only those affected error records after deploying the change. From the grader directory, edit the assignment, task and student IDs below for your case. The command holds the grader lock, refuses to reset a task with jobs in flight, and backs up the state before changing it. The next grader run regrades the selected submissions; the derived leaderboard is refreshed when that run publishes.

```bash
uv run python - <<'PY'
from datetime import datetime, timezone
from pathlib import Path
from shutil import copyfile
from config import ConfigParser
from grader.grading_state import GradingState
from grader.utils import exclusive_lock

assignment, task = "mpi-matmul", "test-cpu"
students = ["2175401", "2012601", "2049617"]
parser = ConfigParser("grader-config.yaml")
parser.parse()
work = Path(parser.config.grader.working_dir)
with exclusive_lock(work / "grader.lock"):
    state = GradingState(work / "grading_state.json")
    if state.task_jobs(assignment, task):
        raise SystemExit("Task still has tracked jobs; let the grader reconcile them first")
    backup = state.path.with_name("grading_state.backup-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f") + ".json")
    copyfile(state.path, backup)
    for student in students:
        tasks = state.records.get(assignment, {}).get(student, {})
        record = tasks.get(task)
        if record and record["status"] == "error" and record["error"].startswith("Grading failed 3 times without producing a result"):
            del tasks[task]
            state.attempts.get(assignment, {}).get(student, {}).pop(task, None)
            print("Reset", assignment, task, student)
    state.save()
    print("Backup:", backup)
PY
```

## Setting Up as a Recurrent Task

To run the grader automatically at regular intervals without administrator privileges, you can use `cron` (Linux/macOS) or `systemd` user timers.

### Option 1: Using Cron

1. Open your crontab for editing:
   ```bash
   crontab -e
   ```

2. Add a cron job to run the grader. Examples:

   **Run every day at 2 AM:**
   ```cron
   0 2 * * * cd /path/to/grader && /usr/bin/uv run main.py >> /path/to/grader/cron.log 2>&1
   ```

   **Run every 6 hours:**
   ```cron
   0 */6 * * * cd /path/to/grader && /usr/bin/uv run main.py >> /path/to/grader/cron.log 2>&1
   ```

   **Run every Monday at 9 AM:**
   ```cron
   0 9 * * 1 cd /path/to/grader && /usr/bin/uv run main.py >> /path/to/grader/cron.log 2>&1
   ```

3. Make sure to:
   - Use absolute paths for both the grader directory and the `uv` executable
   - Use an absolute path for `worker_private_key` in `grader-config.yaml`, or keep it relative to the grader directory the job `cd`s into

### Option 2: Using Systemd User Timer

Systemd user timers don't require root privileges and offer more control.

1. Create a service file at `~/.config/systemd/user/grader.service`:

   ```ini
   [Unit]
   Description=MCPGrader
   
   [Service]
   Type=oneshot
   WorkingDirectory=/path/to/grader
   ExecStart=/usr/bin/uv run main.py
   
   [Install]
   WantedBy=default.target
   ```

2. Create a timer file at `~/.config/systemd/user/grader.timer`:

   ```ini
   [Unit]
   Description=Run MCPGrader daily
   
   [Timer]
   OnCalendar=daily
   Persistent=true
   
   [Install]
   WantedBy=timers.target
   ```

3. Enable and start the timer:

   ```bash
   systemctl --user daemon-reload
   systemctl --user enable grader.timer
   systemctl --user start grader.timer
   ```

4. Check timer status:

   ```bash
   systemctl --user list-timers
   systemctl --user status grader.timer
   ```

**Timer Schedule Examples:**

- `OnCalendar=daily` - Run once per day at midnight
- `OnCalendar=*-*-* 02:00:00` - Run daily at 2 AM
- `OnCalendar=Mon *-*-* 09:00:00` - Run every Monday at 9 AM
- `OnCalendar=*-*-* 00/6:00:00` - Run every 6 hours

## Test Script Format

Your test scripts must output results in the following JSON format on the **last line** of stdout:

```json
{"passed": 12, "total": 12, "times": [442.44, 664.42, 886.58]}
```

**Required Fields:**

- **`passed`**: Number of tests passed (integer)
- **`total`**: Total number of tests (integer)
- **`times`**: Array of runtime measurements in milliseconds (array of floats)

**Example Test Script Output:**

```bash
#!/bin/bash
# ... test execution ...
echo "Test 1: PASSED"
echo "Test 2: PASSED"
echo "Test 3: FAILED"

# Last line must be JSON
echo '{"passed": 2, "total": 3, "times": [123.45, 234.56, 345.67]}'
```

## Output Format

The grader produces a JSON file (specified in `grades_file`) with the following structure:

```json
{
  "assignment-name": [
    {
      "name": "123456",
      "commit_hash": "abc1234",
      "status": "graded",
      "error": "",
      "stdout": "Test output...",
      "avg_runtime": 123.456,
      "data": {
        "passed": 10,
        "total": 10,
        "times": [100.0, 120.5, 150.3]
      }
    }
  ]
}
```

**Field Descriptions:**

- **`name`**: The student ID from the worker submission
- **`commit_hash`**: Git commit hash (first 7 characters) of the graded submission
- **`status`**: Grading status (`"graded"` or `"error"`)
- **`error`**: Error message if status is `"error"`, empty otherwise
- **`stdout`**: Complete output from the test script
- **`avg_runtime`**: Average of all runtime values from the `times` array (in milliseconds)
- **`data`**: The parsed JSON output from the test script (see [Test Script Format](#test-script-format))

---

## Troubleshooting

**Issue: "Grader worker request ... failed (401)"**
- The grader machine's clock is more than 60 seconds off. Sync it (e.g. with NTP).

**Issue: "Grader worker request ... failed (403)"**
- `worker_private_key` isn't authorized on the worker. Run `scripts/admin-keys.sh list` in the worker repository and `push` the key.

**Issue: "Failed to get commit hash for submission ..."**
- The deploy key was rejected, or the repository URL is wrong. Check that the student added the public key under **Settings → Deploy keys** of that exact repository that the worker returned for it.

**Issue: "working_dir does not exist"**
- Create the directory specified in `working_dir` before running: `mkdir -p /tmp/grader`

**Issue: Test script not found**
- Verify the `test_script_path` points to an existing file
- Use absolute paths or ensure relative paths are correct from the grader directory

**Issue: Test results not showing in output**
- Ensure your test script outputs valid JSON on its last line
- Check that the JSON format matches exactly: `{"passed": X, "total": Y, "times": [...]}`
- Review test script stdout in the leaderboard JSON for debugging

**Issue: Cron job not running**
- Check cron logs: `grep CRON /var/log/syslog` (Linux) or `log show --predicate 'process == "cron"' --last 1h` (macOS)
- Verify absolute paths in the crontab entry
- Ensure the cron output log file is writable

---

## License

This project is licensed under the GNU General Public License v3.0 - see the [LICENSE](LICENSE) file for details.

Copyright (C) 2026

This program is free software: you can redistribute it and/or modify it under the terms of the GNU General Public License as published by the Free Software Foundation, either version 3 of the License, or (at your option) any later version.

This program is distributed in the hope that it will be useful, but WITHOUT ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the GNU General Public License for more details.
