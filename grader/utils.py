import errno
import fcntl
import json
import os
import socket
import shlex
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any

def write_json_atomic(path: Path, data: Any, indent: int | None = None) -> None:
    """Write via a temp file and rename, so a crash never leaves a half-written file behind."""
    tmp_path = path.with_name(path.name + ".tmp")
    with open(tmp_path, 'w') as f:
        json.dump(data, f, indent=indent)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_path, path)

    # Persist the rename itself
    dir_fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)

@contextmanager
def deploy_key_env(deploy_key: str):
    """Write the submitted private key to a 0600 temp file and yield the env that makes git use it."""
    key = deploy_key.replace("\r\n", "\n").strip() + "\n"  # ssh rejects keys without a trailing newline

    fd, path = tempfile.mkstemp(prefix="deploy_key_")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(key)
        yield {
            "GIT_SSH_COMMAND": f"ssh -F /dev/null -i {shlex.quote(path)} -o IdentitiesOnly=yes -o BatchMode=yes -o StrictHostKeyChecking=accept-new"
        }
    finally:
        os.unlink(path)

class LockHeldError(Exception):
    """Another process holds the lock; the message says who."""

@contextmanager
def exclusive_lock(path: Path):
    """Hold an exclusive POSIX lock on path for the block,
    or raise LockHeldError at once if another process holds it. The kernel releases it when the
    process dies, even by SIGKILL, so a killed run never leaves a stale lock behind."""
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.lockf(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as e:
            if e.errno not in (errno.EACCES, errno.EAGAIN):
                raise
            with open(path, 'r') as f:  # its own descriptor: closing it doesn't release a lock we don't hold
                raise LockHeldError(f"{path} is held by {f.read().strip() or 'an unknown process'}") from None

        # notify who the owner of the lock is 
        os.ftruncate(fd, 0)
        owner = f"pid {os.getpid()} on {socket.gethostname()}"
        if os.environ.get("SLURM_JOB_ID"):
            owner += f", SLURM job {os.environ['SLURM_JOB_ID']}"
        os.pwrite(fd, (owner + "\n").encode(), 0)
        yield
    finally:
        os.close(fd)
