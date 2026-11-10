"""Internal source snapshot support; not part of the grading-script interface."""

import hashlib
from pathlib import Path


def snapshot_sources() -> dict[str, bytes]:
    root = Path(__file__).parent
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*.py"))
    }


def source_hash(sources: dict[str, bytes]) -> str:
    digest = hashlib.sha256()
    for name, contents in sorted(sources.items()):
        digest.update(name.encode())
        digest.update(b"\0")
        digest.update(hashlib.sha256(contents).digest())
    return digest.hexdigest()
