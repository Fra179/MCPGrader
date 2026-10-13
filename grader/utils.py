import json
import os
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
    os.replace(tmp_path, path)

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
