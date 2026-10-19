# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

Python >=3.14, managed with `uv`. Regression checks use standard-library `unittest`; no linter is configured.

- `uv sync` — install dependencies
- `uv run python -m unittest discover -s tests -v` — run the local regression checks (local Git repositories, no worker or SLURM required)
- `uv run main.py` — run the grader (reads `grader-config.yaml` from the cwd; copy from `grader-config-example.yaml`)
- `uv run python -m grader_worker.GraderWorkerApi <worker URL> <admin key file> [assignment]` — sanity-check the worker URL and admin key by printing submissions

`grader-config.yaml`, `*.json` (incl. `leaderboard.json`), `keys/`, `*.lock` and everything under `test_scripts/` are gitignored. Config validation (`assert_valid`) requires `working_dir`, the `worker_private_key` file, each `test_script_path` and any configured per-task `setup_script` to already exist.

## Architecture

The grader is meant to run on a SLURM cluster: `main.py` → `Grader.grade()` ([grader/grader.py](grader/grader.py)). `main.py` first checks with the user's `lock_status` ([check_locks.py](check_locks.py)) that POSIX locks work in `working_dir` and, on Lustre/NFS, are coherent across nodes.

**One run at a time per `working_dir`:** `grade()` holds `exclusive_lock(<working_dir>/grader.lock)` ([grader/utils.py](grader/utils.py)) for the whole run: `fcntl.lockf` (POSIX, the kind `check_locks.py` verifies; on NFS `flock` can be node-local where `lockf` isn't), non-blocking, so a second run logs the holder (pid, host, SLURM job, written into the file for information only) and returns. The kernel releases it when the process dies. POSIX locks are dropped when the process closes *any* descriptor of the file, so nothing else in the process may open `grader.lock`. The grading state is loaded only once the lock is held.

The grader works in **passes** (`Grader._pass`); `grader.wait_for_jobs` (default `true`) repeats them every `Grader.poll_interval` (30 s) until no job is in flight and nothing was deferred, `false` makes one pass and exits (for a run started on a schedule). Nothing lives only in the process: the jobs in flight are kept in the grading state, so a run can be killed at any point and the next one carries on. A pass:

1. `_drop_finished_jobs`: asks `runner.finished_jobs` ([runners/SlurmRunner.py](runners/SlurmRunner.py): one `sacct -X` call; locally, the job's pid) about every job in the grading state, logs those that didn't end `COMPLETED`, and removes them. A job `sacct` doesn't know yet (`UNKNOWN`) stays in flight for `UNKNOWN_JOB_GRACE` (10 min) after submission; if `sacct` fails, every job stays in flight for this pass.
2. `_ingest_results`: reads every result file under `<working_dir>/results/<assignment>/<task>/` (written by jobs still running, finished, or started by an earlier run), `record`s each into the grading state with the file's `perf_hash`, saves the grading state (`<working_dir>/grading_state.json`, atomic) and only then deletes the files (a crash in between just records them again). Files without `perf_hash` (older format) are discarded, so those students are regraded. Runs after step 1, so a finished job's files are complete when read.
3. `_submit_assignment`, per assignment: the first pass of a run takes a snapshot (`_snapshot`): `GraderWorkerAPI` ([grader_worker/](grader_worker/)) lists submissions from the grader Cloudflare Worker (`POST /submissions` with `assignment_name` = the assignment's `name`; requests are signed with the ECDSA P-256 admin key in `worker_private_key`, raw `r||s` signature over `METHOD\npath\ntimestamp\nsha256(body)`, 60 s of clock skew allowed; it returns the normalized `repo_ssh_url` and the generated private deploy key), and `git ls-remote HEAD` resolves each submission with its deploy key (up to 3 attempts on failure). Successful hashes remain fixed for the run; unresolved submissions are skipped and their lookups retried on later passes or runs. For each task, a submission needs grading if `GradingState.needs_grading` ([grader/grading_state.py](grader/grading_state.py)) says so (no record for that task with the same commit, the same repo URL and the task's current `performance_hash()`) and no job in flight already covers that student and task. `grader.job_mode` splits a task's updated submissions: `per_task` (default) puts them all in one job, `per_submission` gives each its own job. Jobs are submitted while fewer than `max_submitted_jobs` are in flight (counting earlier runs' jobs); the rest are deferred to a later pass, so nothing is queued in memory. Each submitted job is added to the grading state and saved right away. A job `sbatch` refuses is logged and left for the next run (it doesn't keep a waiting run going). `blocking`: while a blocking task has jobs in flight, deferred or refused, the assignment's later tasks aren't submitted (they're still checked, and logged as waiting). **Attempt cap:** each submitted job adds an attempt per student (`GradingState.attempts`, keyed by commit and `perf_hash`, so a new commit or task change resets it; `record` clears it). With `MAX_GRADING_ATTEMPTS` (3) reached, `_give_up_if_exhausted` records an `error` result instead of submitting, rather than trying indefinitely. Submissions with an unresolved HEAD are not submitted and consume no grading attempts. Without the cap, a waiting run would resubmit a student whose job always dies every pass, forever.
4. Before submission, the coordinator downloads the pinned repository using its deploy key and creates an atomic Git bundle via `RepositoryStaging` ([grader/repository_staging.py](grader/repository_staging.py)); it fetches the pinned commit explicitly if needed. Bundles are keyed by URL and commit, shared across tasks, and tracked in each job's optional `bundles` list. After submissions, each pass removes bundles not referenced by in-flight jobs. Download failures skip that repository/commit for the rest of the run, consume no grading attempts and retry next run. Final HEAD lookup failures and bundle failures notify Sentry once per submission/stage/run without exception locals or deploy keys.
   Inside the job (`SubmissionGrader.grade_all`, [grader/submission_grader.py](grader/submission_grader.py), one instance per task, submitted by `SlurmRunner.submit` via `submitit` with the task's `slurm_backend.config` as parameters), only SLURM rank 0 works. Each repo is cloned from its local bundle and explicitly checked out at its snapshotted commit, without internet access or deploy keys. The task's test script is copied in and run in its own process group (the group is killed after the task's `submission_timeout`, giving status `timeout`), and the **last stdout line** is parsed as JSON (`{"passed", "total", "times"}`). Each result is written atomically to `<working_dir>/results/<assignment>/<task>/<student_id>.json` as soon as it's ready, and the clone is deleted right after unless `preserve_repo_files`; the job returns nothing. Each result carries the `perf_hash` its job was submitted with (`SubmissionGrader` computes it in the main process).
5. `_publish`, once per run, after the last pass (not part of `_pass`): writes `grades_file` as `grading_state.export_leaderboard(config)`, then optionally pushes it via `pusher/github_pusher.py` (the only place a GitHub token is used; it skips the commit if the file in the repo is identical, which it can't check above 1 MB). A job that fails or times out only loses the submission it was grading: the results it wrote are recorded, and students without one are graded again.

Things that span files:
- **Grading state** (`GradingState`): the grader's only persistent state. One record per (assignment, student id, task) with the latest result plus `commit_hash`, `repo_url`, `perf_hash` and `graded_at`, and `jobs`: the SLURM jobs in flight (`JobRecord`: job id, assignment, task, student ids, `perf_hash`, `submitted_at`); a result and its commit are saved in one atomic write, so "what's graded" and "what's on the leaderboard" can't disagree. `grades_file` is a derived export (never read back). Students not regraded keep their records. Records are keyed by student id (as a string), so a student switching repos replaces their record.
- **Regrading**: `AssignmentTaskConfig.performance_hash()` (task name, script path *and contents*, SLURM config, reduction, submission_timeout, optional setup script path and contents) at submission time is stored in each record (a result graded with an older script is recorded as such, and regraded); when it changes, every submission of that task is regraded (old results stay on the leaderboard until replaced). Omitting setup preserves the previous hash.
- **Task environment setup**: optional `setup_script` is resolved to an absolute path in `SubmissionGrader.__init__` in the coordinator. For each submission, `_run_test_script` launches `bash -l`, enables `errexit` after login initialization, sources the trusted original setup file, then `exec`s the copied grading script with positional arguments for safe path handling. Exported environment variables reach Python scripts. Setup shares the clone cwd, output pipes, process group, and `submission_timeout` with grading; failures are recorded as normal error results under the current exit-code contract. No setup means the previous direct launch. Compute nodes must see the setup file at the same path. Legacy job objects without the new attribute use the previous direct launch.
- **Aggregations**: `aggregation/` holds `ABAggregation` subclasses registered in `AGG_NAME_TO_CLASS` (`mean`, `median`). A task's optional `reduction` selects one; `export_leaderboard` looks it up in the current config (mean if unset or the task is gone) to compute per-task `avg_runtime`. Neither result files nor grading state records store it. New aggregations must be added to `AGGS_LIST`.
- **Result shape**: the leaderboard JSON is per assignment, one entry per student with per-task sub-dicts (`commit_hash`, `status`, `error`, `stdout`, `runtimes`, `avg_runtime`, `data`, `reduction`, plus `name` and `tasks`), the same format the removed `GradeResult` produced. Keep it stable: it's the file pushed to the leaderboard repo. README's "Output Format" section is outdated in this respect.
- The job is a bound method of `SubmissionGrader`, which holds only data (task config, working dir, flags), so only that and the job's arguments (submissions with blank deploy keys, results dir, snapshotted commit hashes, local bundle paths) land in `slurm_logs/*_submitted.pkl`, never `Grader` or its config (GitHub PAT). Legacy pickles still contain deploy keys, and legacy jobs still clone remotely. Don't submit bound methods of `Grader`, and don't give `SubmissionGrader` references to config or `Grader` state. `slurm_logs/` is chmod 0700.

Note: the old `gh/` (GitHub API) and `google_form/` (Google Forms) submission sources were removed in favor of `grader_worker/`.

## Work status (as of 2026-10-14)

### Update 2026-10-16: coordinator downloads for offline compute nodes

Verified on Leonardo: DCGP node `lrdn4990` cannot reach GitHub on ports 22 or 443; `lrd_all_serial` allocated `login13`, where GitHub HTTPS and SSH work. Worker access, `sbatch`/`sacct`, and shared locks still need verification there.

Implemented locally: coordinator-side pinned Git bundles, credential-free new job arguments, bundle retention in the job ledger across restarts, cleanup after the final referencing job finishes, and explicit Sentry events for failed HEAD lookups/downloads. Failures retry next run without consuming grading attempts or replacing grades. Local regression tests cover offline execution, serialization, restart retention, shared-bundle cleanup, job limits, partial download failures, and Sentry event contents. No cluster execution of this change yet. Existing attempt-cap errors for students 2175401, 2012601 and 2049617 in `mpi-matmul[test-cpu]` need a targeted reset on Leonardo after deploying; don't delete the whole grading state.

The historical descriptions below of deploy keys in new job pickles and fetching inside new jobs are superseded by this update. No commits were created.

The grader runs on CINECA Leonardo. Everything below was tested only locally (submitit falls back to its local executor without SLURM), never on the cluster.

### Uncommitted

Restartable runs, steps 1 to 3 (below): `grader/grader.py` (rewritten around passes), `grader/grading_state.py`, `grader/submission_grader.py`, `grader/structs.py`, `runners/ABRunner.py`, `runners/SlurmRunner.py` (local queue, `wait`, `wait_all` and `collect_results` removed; `submit` and `finished_jobs` added), `config/configs.py` (`wait_for_jobs`), `pusher/github_pusher.py` (skips identical pushes), `README.md`, `grader-config-example.yaml`, this file. `grader/utils.py` (`exclusive_lock`). Also uncommitted: the user's `check_locks.py` and its check in `main.py` (moved after config parsing, now checking `working_dir`).

Tested locally: a run killed mid-job, whose orphaned job kept writing results, was picked up by the next run (step 1 alone, and with the job list: the next run waited for the orphan instead of resubmitting); a stale-`perf_hash` leftover was recorded then regraded; a leftover without `perf_hash` was discarded; `per_submission` with `max_submitted_jobs: 1` ran the two jobs one after the other; a blocking task held back the next task until its job ended; `wait_for_jobs: false` exited right after submitting and a later run recorded the results; a run started during another exited naming the holder, and one started after the holder was `kill -9`ed took over its job; a student whose job killed itself got an `error` result after 3 jobs (the other student was graded, the next run left it alone); an unreachable repository was skipped after 3 jobs and the run ended. The user commits themselves; don't commit unless asked. Their git signs commits with GPG, so test repos created during tests need `-c commit.gpgsign=false`.

### Restartable runs, as short reconciliation passes (implemented, uncommitted)

Goal: run the grader's main process on Leonardo's `lrd_all_serial` partition (budget free, 4 h walltime, 4 cores / ~30 GB per user, no GPUs) as a job that reschedules itself every ~10 minutes (`sbatch --begin=now+10minutes`, or `scrontab` if enabled), instead of from another machine over SSH: Leonardo's SSH login needs 2FA with 12 h certificates, so unattended SSH isn't possible. Each pass ingests results, checks jobs in flight, submits what's needed and exits, without waiting for jobs; students show up on the leaderboard at the end of the first run after their result file is written. Unverified on Leonardo: that the serial node can reach the worker and GitHub and can run `sbatch`/`sacct`.

1. Done (uncommitted): ingest leftover result files; `perf_hash` travels with the job.
2. Done (uncommitted), as described in the flow. Original plan: a ledger of submitted jobs (SLURM job id, assignment, task, student ids, `perf_hash`), stored **inside `grading_state.json`** so it's saved in the same atomic write. A pass drops finished jobs (via `sacct`; empty answer right after submission = still running; unknown for longer than its walltime = finished) and skips students covered by a job in flight. Submits up to `max_submitted_jobs` minus jobs in flight; the rest wait for a later pass, so the local queue needs no persistence. `SlurmRunner` gains `submit()` and `is_done(job_id)` across processes. `blocking` (decided with the user): a pass doesn't submit the assignment's later tasks while a blocking task has jobs in flight (submitted or waiting for the job cap); a later pass submits them once those jobs are done. Later tasks are still checked for updates, just not submitted. Testing turned out not to need a fake runner: locally, the job id is the job's pid.
3. A lock with `fcntl.flock` on a file in `working_dir` (Leonardo's Lustre is mounted with `flock`, so locks are coherent across nodes and released when the process dies), and a per-(student, commit, perf_hash) attempt cap so a submission that keeps killing its job gets an `error` result. Done (uncommitted), as described above; the lock uses `lockf`, not `flock`. Why the lock matters: a run that loaded the grading state before another run's save doesn't see results that run ingested (and deleted), grades those students again, and its later save overwrites the other's records.

Next: run it on Leonardo. Check from `srun -p lrd_all_serial --pty bash` that the serial node reaches the worker and GitHub and can run `sbatch`/`sacct`, that `lock_status` says `cluster` for `working_dir` there, then set up the self-rescheduling job (`wait_for_jobs: false`).

Decided: keep JSON, not SQLite (one file with the ledger inside gives the same atomicity; jobs keep writing result files either way).

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

Done (uncommitted), independent of the feasibility result: `SubmissionGrader._grade_cloned` unlinks the grading-script destination before copying, replacing student-committed symlinks without changing their targets. Regression checks cover both existing and missing targets, including target contents and permissions.

Do now, independent of the feasibility result (agreed, not done):

- Keep the worker admin key and `grader-config.yaml` where compute nodes can't read them. Only the main process needs them; on Leonardo, `$HOME`, `$WORK` and `$SCRATCH` are all visible from compute nodes.

Not solved by containers: the result is the last stdout line, which student processes can forge (e.g. a background process started by `make` printing JSON). Fixing that needs a different contract, such as the test script writing the result to a file the student can't write.

### Pin grading jobs to the submission snapshot (implemented, uncommitted)

- `_submit_task` passes a per-batch map of repo URLs to snapshotted commit hashes to `SubmissionGrader.grade_all`. Jobs explicitly check out that commit, fetching it first if the clone doesn't contain it. An unavailable commit fails grading rather than falling back to HEAD.
- HEAD lookups get up to three attempts before submission. Unresolved submissions are skipped and retried on later passes or runs; they don't keep a waiting run alive on their own. Successful lookups remain fixed across all tasks for the run.
- Jobs pickled before this change may still call `grade_all` without the commit map; only those legacy jobs retain the previous HEAD behavior.
- Verified with local Git repositories: pushes between snapshot and clone, both job modes, blocking tasks grading the same commit, later runs picking up new commits, transient and persistent HEAD lookup failures, explicit fetching after branch history is replaced, and unavailable commits reaching the grading-attempt cap. Also checked with submitit's local executor that the commit map survives job serialization and the run settles and publishes. Not tested on Leonardo.

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

### TODO: avoid clone-directory collisions (not urgent)

- Replace the repository-name flattening in `SubmissionGrader.repo_dir` with a collision-resistant directory identity, such as the worker submission UUID plus task identity. Distinct repositories like `course_group/solution` and `course/group_solution` currently map to the same directory for the same task.
- Verify that such repositories use separate clone directories and that one job's cleanup cannot remove the other's clone.

### TODO comments in the code (agreed, deferred)

- `submission_grader.py`: exit-code contract (0 graded, 1 student failure, 2 infrastructure failure); don't cache 2.
- `grader.py`: deploy keys stay in submitit's pickles.
- `grading_state.py`: drop students no longer in the worker and tasks no longer in the config (in `export_leaderboard`); SQLite instead of the JSON file, same interface (no immediate need; Leonardo's Lustre has coherent `flock` so SQLite's locking works, but not WAL mode).

### Architecture discussion to resume (saved 2026-10-15; proposals, not approved for implementation)

The current module layout is suitable for the project's size. Preserve the separation between `Grader` (coordination), `SubmissionGrader` (job execution without the admin key or GitHub PAT), `GradingState` (persistent records, attempts and jobs; derived leaderboard), and `SlurmRunner` (scheduler details). The explicit reconciliation-pass order is a strength. `Grader` being about 350 lines does not itself justify splitting it.

Ideas discussed:

1. **Represent a pinned submission as one value.** A small `PinnedSubmission` dataclass containing `submission` and `commit_hash` could replace parallel submission lists and URL-keyed commit maps at the job interface. This would make repository/commit consistency easier to maintain and simplify job arguments.
2. **Allow dependencies to be supplied for tests.** `Grader` currently constructs its runner, worker client and pusher internally. Supplying those dependencies, with normal production defaults, could reduce constructor/private-method patching in tests.
3. **Make state-changing decisions explicit.** This is the user's preferred topic for the next discussion. In `_submit_assignment`, replace the `updated_submissions` comprehension with a regular loop: skip students in flight, call `_give_up_if_exhausted` explicitly (it can record an error and save state), then append eligible submissions. Preserve the current ordering and behavior.
4. **Simplify aggregations eventually (low priority).** A name-to-function dictionary could replace the current abstract class and subclasses; `pretty_name` and `compatible_with` are unused.

For item 3, also split `_snapshot` into creation and retry operations, with their effects visible directly in `_submit_assignment`:

```python
if assignment_cfg.name not in self.snapshots:
    self._create_snapshot(assignment_cfg)
else:
    self._retry_unresolved_commits(assignment_cfg.name)

submissions, commit_hashes = self.snapshots[assignment_cfg.name]
```

The helpers would contain the existing first-call and later-call logic. Successful commit hashes must remain fixed; only unresolved lookups are retried. Do not introduce an extra retry on the initial call. Leave `_pass` and `_ingest_results` structurally unchanged: their operation order and save-before-delete behavior are already explicit. These proposals are readability/refactoring work, not changes to grading policy. Discuss before implementing.

### Possible improvements (not planned)

- A private full-results file, pushing only summary fields (the pushed leaderboard has every student's full stdout/error). The comment in `grade()` says so.
- From an architecture review (its "grading ledger" is done, as `GradingState`): aggregations as a name→function dict instead of the class hierarchy (`pretty_name`/`compatible_with` are unused). A second runner adapter (submitit's `cluster="debug"`) only if a test suite is added.
- A configurable polling interval (30 s, hardcoded).

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
- set `grader.poll_interval` low (e.g. 1) for `wait_for_jobs` tests, and make the test script's duration controllable (an env var is inherited by the local job)
- to simulate a killed run, kill the Python process itself (`pkill -9 -f "python run.py"`): killing `uv run` leaves its Python child running (the lock then makes the next run exit)
- to make a job die, have the test script `kill -9 $PPID` (the job's process) for one student; for an unknown commit, give the fake submission a nonexistent `file://` URL
- the user's shell is zsh: loops relying on word splitting need `bash`
- don't pipe the grader's output when timing a single pass: local jobs inherit its stdout, so the pipe stays open until they end
