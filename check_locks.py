import fcntl
import os
import tempfile


def _unescape(s):
    # /proc/mounts escapes spaces, tabs, etc. as octal (\040)
    return s.encode().decode("unicode_escape")


def find_mount(path):
    """Return (mountpoint, fstype, options) for the filesystem containing path."""
    path = os.path.realpath(path)
    best = None
    with open("/proc/mounts") as f:
        for line in f:
            _, mnt, fstype, opts, *_ = line.split()
            mnt = _unescape(mnt)
            if path == mnt or path.startswith(mnt.rstrip("/") + "/"):
                # longest prefix match wins (e.g. /mnt vs /mnt/subdir)
                if best is None or len(mnt) >= len(best[0]):
                    best = (mnt, fstype, set(opts.split(",")))
    return best


def lock_status(path="."):
    """
    Return one of:
      'cluster'  - locks are coherent across nodes
      'local'    - locks work but only within this node
      'none'     - locking is not supported
      'ok'       - not a network fs; normal local locking
    """
    res = find_mount(path)

    if not res:
        raise RuntimeError(f"Could not find mount point for {path}")

    _, fstype, opts = res

    if fstype == "lustre":
        if "flock" in opts:
            mode = "cluster"
        elif "localflock" in opts:
            mode = "local"
        else:
            mode = "none"  # noflock or unspecified
    elif fstype.startswith("nfs"):
        local_lock = next((o.split("=", 1)[1] for o in opts if o.startswith("local_lock=")), "none")
        if "nolock" in opts or local_lock in ("all", "posix"):
            mode = "local"
        else:
            mode = "cluster"
    else:
        mode = "ok"

    # make sure the kernel actually grants a POSIX lock
    if mode != "none":
        try:
            with tempfile.NamedTemporaryFile(dir=path) as tmp:
                fcntl.lockf(tmp, fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.lockf(tmp, fcntl.LOCK_UN)
        except OSError:
            mode = "none"

    return mode


if __name__ == "__main__":
    status = lock_status(os.getcwd())
    if status not in ("cluster", "ok"):
        raise SystemExit(f"Unsafe locking on {os.getcwd()}: {status}")
    print(f"Locking OK ({status})")