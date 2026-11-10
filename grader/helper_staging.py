"""Publish pinned helper sources outside student clones before job submission."""

from pathlib import Path
import tempfile

from grader_helpers._distribution import source_hash


def stage_helpers(working_dir: Path, sources: dict[str, bytes]) -> Path:
    directory = working_dir.resolve() / "helper_packages"
    directory.mkdir(parents=True, exist_ok=True)
    destination = directory / source_hash(sources)
    if destination.is_dir():
        return destination

    with tempfile.TemporaryDirectory(prefix="stage_", dir=directory) as temporary:
        root = Path(temporary) / "package"
        package = root / "grader_helpers"
        package.mkdir(parents=True)
        for name, contents in sources.items():
            path = package / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(contents)
        # The coordinator holds the working-directory lock during normal submission.
        root.rename(destination)
    return destination
