# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

Python >=3.14, managed with `uv`. There is no test suite or linter configured.

- `uv sync` — install dependencies
- `uv run main.py` — run the grader (reads `grader-config.yaml` from the cwd; copy from `grader-config-example.yaml`)
- `uv run python -m grader_worker.GraderWorkerApi <worker URL> <admin key file> [assignment]` — sanity-check the worker URL and admin key by printing submissions

`grader-config.yaml`, `*.json` (incl. `leaderboard.json`), `keys/`, `*.lock` and everything under `test_scripts/` are gitignored. Config validation (`assert_valid`) requires `working_dir`, the `worker_private_key` file and each `test_script_path` to already exist.

## Architecture

The grader is meant to run on a SLURM cluster: `main.py` → `Grader.grade()` ([grader/grader.py](grader/grader.py)).

Flow:
1. For each assignment, `GraderWorkerAPI` ([grader_worker/](grader_worker/)) lists submissions from the grader Cloudflare Worker (`POST /submissions` with `assignment_name` = the assignment's `name`). Requests are signed with the ECDSA P-256 admin key in `worker_private_key` (raw `r||s` signature over `METHOD\npath\ntimestamp\nsha256(body)`; the worker allows 60 s of clock skew). The worker returns the normalized `repo_ssh_url` and the generated private deploy key.
2. In the main process, `git ls-remote HEAD` is run once per submission (with its deploy key) and checked with `GradingState.needs_grading` ([grader/grading_state.py](grader/grading_state.py)): a submission needs grading unless its grading-state record for that task has the same commit, the same repo URL and the task's current `performance_hash()`. Tasks with no updated submissions get no job. `grader.job_mode` decides how a task's updated submissions are split: `per_task` (default) puts them all in one job, `per_submission` gives each its own job. The cluster caps the number of jobs per user, so `per_submission` should be paired with `grader.max_submitted_jobs`.
3. For each task with updates, `SlurmRunner` ([runners/SlurmRunner.py](runners/SlurmRunner.py), via `submitit`) submits `SubmissionGrader.grade_all` ([grader/submission_grader.py](grader/submission_grader.py), one instance per task) with the updated submissions (all of them, or one per job), using the task's `slurm_backend.config` as submitit parameters. `blocking` tasks are waited on immediately; otherwise all jobs are awaited with `wait_all()`. With `max_submitted_jobs` set, `run()` only queues jobs past the limit, and `wait`/`wait_all` submit them as earlier jobs finish (polling every 30 s); a job `sbatch` refuses is recorded and raised by `collect_results`. `Grader.job_ids` holds one entry per task with the list of its job ids.
4. Inside the job, only SLURM rank 0 works. Each repo is cloned over SSH with the student's deploy key (`deploy_key_env` in [grader/utils.py](grader/utils.py) writes it to a 0600 temp file and sets `GIT_SSH_COMMAND`), the task's test script is copied in and run in its own process group (the group is killed after the task's `submission_timeout`, giving status `timeout`), and the **last stdout line** is parsed as JSON (`{"passed", "total", "times"}`). Each result is written atomically to `<working_dir>/results/<assignment>/<task>/<student_id>.json` as soon as it's ready, and the clone is deleted right after unless `preserve_repo_files`; the job returns nothing. That directory is cleared before each job is submitted.
5. `_record_results` reads those result files (even if the job failed, so a timeout or crash only loses the submission in progress) and `record`s each into the grading state. `grade()` then saves the grading state (`<working_dir>/grading_state.json`, atomic), writes `grades_file` as `grading_state.export_leaderboard(config)`, and only then optionally pushes the leaderboard via `pusher/github_pusher.py` (the only place a GitHub token is used).

Things that span files:
- **Grading state** (`GradingState`): the grader's only persistent state. One record per (assignment, student id, task) with the latest result plus `commit_hash`, `repo_url`, `perf_hash` and `graded_at`; a result and its commit are saved in one atomic write, so "what's graded" and "what's on the leaderboard" can't disagree. `grades_file` is a derived export, rewritten every run (never read back). Students not regraded keep their records. Records are keyed by student id (as a string), so a student switching repos replaces their record.
- **Regrading**: `AssignmentTaskConfig.performance_hash()` (task name, script path *and contents*, SLURM config, reduction, submission_timeout) is stored in each record; when it changes, every submission of that task is regraded (old results stay on the leaderboard until replaced).
- **Aggregations**: `aggregation/` holds `ABAggregation` subclasses registered in `AGG_NAME_TO_CLASS` (`mean`, `median`). A task's optional `reduction` selects one; `export_leaderboard` looks it up in the current config (mean if unset or the task is gone) to compute per-task `avg_runtime`. Neither result files nor grading state records store it. New aggregations must be added to `AGGS_LIST`.
- **Result shape**: the leaderboard JSON is per assignment, one entry per student with per-task sub-dicts (`commit_hash`, `status`, `error`, `stdout`, `runtimes`, `avg_runtime`, `data`, `reduction`, plus `name` and `tasks`), the same format the removed `GradeResult` produced. Keep it stable: it's the file pushed to the leaderboard repo. README's "Output Format" section is outdated in this respect.
- The job is a bound method of `SubmissionGrader`, which holds only data (task config, working dir, flags), so only that and the job's arguments (submissions with their deploy keys, results dir) land in `slurm_logs/*_submitted.pkl`, never `Grader` or its config (GitHub PAT). Don't submit bound methods of `Grader`, and don't give `SubmissionGrader` references to config or `Grader` state. `slurm_logs/` is chmod 0700. `SlurmRunner.collect_results` reads `job._sub_jobs[0]` to return only rank 0's result; the grader calls it only to detect a failed job.

Note: the old `gh/` (GitHub API) and `google_form/` (Google Forms) submission sources were removed in favor of `grader_worker/`.

## Work status (as of 2026-10-13)

The grader runs on CINECA Leonardo. Everything below was tested only locally (submitit falls back to its local executor without SLURM), never on the cluster.

### Uncommitted

`grader.job_mode` (`per_task` | `per_submission`) and `grader.max_submitted_jobs` (`-1` = no limit) are implemented, tested and documented, but not committed: `config/configs.py`, `grader/grader.py`, `runners/ABRunner.py`, `runners/SlurmRunner.py`, `README.md`, `grader-config-example.yaml`, this file. Also uncommitted: the grading state, `GradingState` (`grader/grading_state.py` new; `grader/task_cache.py` deleted; `GradeResult` removed from `grader/structs.py`). Its export was checked to be byte-identical to the old `GradeResult` output. Existing `.cache/` files and the old grades file aren't imported: the first run with the grading state regrades everyone. The user commits themselves; don't commit unless asked. Their git signs commits with GPG, so test repos created during tests need `-c commit.gpgsign=false`.

### In progress: sandboxing student code (security)

Student code (the Makefile run by `make`, their binary, anything those start) runs as the grader's Unix user, so today it can read and write everything that user can:

- the worker admin key, which gives every deploy key
- `grader-config.yaml`, which holds the GitHub PAT
- the deploy keys in `slurm_logs/*.pkl` (`0700` doesn't help against the same user)
- other students' clones and result files, and `grades.json`
- test scripts in `test_scripts/`

It can also exfiltrate through stdout, which is pushed to the leaderboard. cgroups don't help (they limit resources, not file access).

Chosen approach: Singularity in the **hybrid model**. Assignments include multi-node MPI with CPU and GPU, so a single container around the whole test script won't work. The test script stays on the host, and every command touching student files runs in a container: `singularity exec --contain [--nv] --bind <clone>:/work --pwd /work <image> make -f Makefile all` for the build, and `srun ... singularity exec ... ./binary` for runs, one container per rank. MPI runs can't use `--containall`/`--pid`: it drops the PMI variables `srun` sets, and separate namespaces break intra-node communication.

Status: feasibility tests are written, not yet run on Leonardo. They're in `test_scripts/singularity_feasibility/` (gitignored, local only; its `README.md` explains the order):

- `00_environment.sh`: Singularity version and config, system bind paths, `srun --mpi=list`, host modules
- `01_isolation.sh`: probes a fake grader layout from inside the container
- `02_kill_cleanup.sh`: process cleanup when the group is killed
- `03_gpu.sbatch`: GPU visibility, nvcc, kernel time, container start-up time
- `04_mpi_hello.sbatch`: 2 nodes, via `srun --mpi=pmix` and host `mpirun`
- `05_mpi_bandwidth.sbatch`: OSU latency and bandwidth, InfiniBand vs fallback
- `image/grader-feasibility.def`: CUDA 12.2 devel, Ubuntu OpenMPI with UCX, OSU 7.3

Unverified guesses in them: partition `boost_usr_prod`, the CUDA version, the OSU download URL, and Ubuntu OpenMPI 4.1.2 working with Leonardo's PMIx and `mpirun`. Next step: the user runs them and shares the output, which decides the Singularity options and the image.

Planned grader changes once feasible (not started):

- per-task `container_image`, `container_options` (default `--contain`) and `container_gpu`; include image path, size and mtime in `performance_hash`
- `SubmissionGrader` passes the full `singularity exec ...` prefix to the test script as env var `GRADER_SANDBOX`
- README: the test-script section currently says to launch with srun/mpirun directly

Rules for migrated test scripts:

- nothing outside the sandbox may execute a file the student can write; only hash or parse it
- build inside the sandbox; compiling outside leaks files via compiler errors (`#include "/path/to/key"`)
- runs must call `srun ... $GRADER_SANDBOX ./binary` directly, not `make run_*`: the student's binary can rewrite the Makefile between runs
- always use `make -f Makefile`, since a student-added `GNUmakefile` takes precedence

Do now, independent of the feasibility result (agreed, not done):

- In `SubmissionGrader._grade_cloned`, `copyfile` onto `grading_script_dest` follows a symlink a student commits under that name, overwriting an arbitrary file of the grader's user (and `chmod` follows it too). Fix: `grading_script_dest.unlink(missing_ok=True)` before copying.
- Keep the worker admin key and `grader-config.yaml` where compute nodes can't read them. Only the main process needs them; on Leonardo, `$HOME`, `$WORK` and `$SCRATCH` are all visible from compute nodes.

Not solved by containers: the result is the last stdout line, which student processes can forge (e.g. a background process started by `make` printing JSON). Fixing that needs a different contract, such as the test script writing the result to a file the student can't write.

### TODO: run artifacts uploaded to the worker (planned, not started)

Assume the worker will provide this endpoint (it doesn't exist yet; `../grader-worker` is the worker's repo):
`PUT /artifacts?submission_id=<worker UUID>&task=<task name>&commit=<sha>`, signed like every admin request (the query string is part of the signed path), body one `.tar.gz` with `Content-Type: application/gzip`, answering `201 {"id": ...}` or `413` if too big. Estimated about a day and ~150 lines on the grader side, plus tests.

1. **Test-script contract**: each run gets an empty folder in env var `GRADER_ARTIFACTS_DIR`; the script writes logs, output files, profiles there. Scripts that ignore it produce no artifacts.
2. **Packing in the job** (`SubmissionGrader`), also after a timeout or error: pack the folder into `results/<assignment>/<task>/<student_id>.tar.gz`. Student code can write there too, so:
   - only regular files: open each with `O_NOFOLLOW` and check it's a regular file after opening, not before (otherwise a planted symlink to `keys/grader.key` gets uploaded)
   - a size cap (`max_artifact_bytes`, default ~50 MB, below Cloudflare's 100 MB request limit); skip files past it and list them in the archive
   - add the worker's `submission_id` (`SubmissionInfo.id`) to the result file, since the main process needs it to upload
3. **Upload in the main process only**, never in the job, because only it may hold the admin key:
   - `GraderWorkerAPI.upload_artifact(...)`: generalize `__post_request` to any method, and add `PUT` to the retry adapter's `allowed_methods`
   - upload after `_retrieve_results`
   - move archives to `<working_dir>/artifacts_pending/` first and delete them only after a successful upload, so a failed upload is retried next run (the cache already marks the commit as graded, and the results folder is emptied before the next job)
   - upload failures never fail grading
4. **Config**: global `upload_artifacts: false` and `max_artifact_bytes`, not in `performance_hash`.
5. **Docs**: README test-script section (how to write artifacts, the limits) and this file.
6. **Tests** (local, with a fake worker recording uploads):
   - artifacts arrive with the right `submission_id`, task and commit
   - a planted symlink is skipped
   - an oversized file is skipped and listed
   - a timed-out run still produces its archive
   - a failed upload is retried on the next run, then cleaned up

With containers, `GRADER_ARTIFACTS_DIR` needs one more `--bind`. Full stdout/stderr could also be added to the archive automatically; that would cover the private-results improvement below.

### TODO comments in the code (agreed, deferred)

- `submission_grader.py`: exit-code contract (0 graded, 1 student failure, 2 infrastructure failure); don't cache 2.
- `grader.py`: restartable runs (ingest leftover result files into the grading state instead of clearing them); deploy keys stay in submitit's pickles.
- `grading_state.py`: drop students no longer in the worker and tasks no longer in the config (in `export_leaderboard`); SQLite instead of the JSON file, same interface (no immediate need; only the main process may open it, since SQLite locking is unreliable on NFS/Lustre).

### Possible improvements (not planned)

- A private full-results file, pushing only summary fields (the pushed leaderboard has every student's full stdout/error). The comment in `grade()` says so.
- From an architecture review (its "grading ledger" is done, as `GradingState`): aggregations as a name→function dict instead of the class hierarchy (`pretty_name`/`compatible_with` are unused). A second runner adapter (submitit's `cluster="debug"`) only if a test suite is added.
- A lock file so two grader runs can't overlap; a configurable polling interval (30 s, hardcoded).

### Test-script fixes suggested to the user (scripts are theirs, in `test_scripts/`)

- Verify the protected-file hashes before `make all`, not after.
- Give the build a timeout and check that it succeeded.
- Optionally re-check the hashes after the last run.
- Adopt the exit-code contract once implemented.

### Decided, don't reopen

- **No chunking, no SLURM job arrays:** the cluster caps jobs per user and array elements count against it. `max_submitted_jobs` handles the cap. No per-assignment job mode.
- **Cache keyed by `ssh_url`, not `student_id`:** the worker enforces unique repo URLs, and (assignment, student_id) is unique.
- **The leaderboard starts blank:** student IDs replaced GitHub usernames, and old entries aren't migrated.

### Testing locally

No SLURM or Singularity here. An end-to-end check of `grade()` that worked:

- replace `grader.grader.GraderWorkerAPI` with a fake returning `SubmissionInfo`s whose `ssh_url` is `file:///path/to/local/repo` (git ignores the SSH settings for `file://`)
- use a throwaway EC key for `worker_private_key` (`openssl ecparam -name prime256v1 -genkey -noout`)
- run with `PYTHONPATH=<repo root>` so submitit's local job processes can import the code
- for throttling tests, set `runner.poll_interval` low and wrap `runner._submit_queued` to record `len(runner.active)`
