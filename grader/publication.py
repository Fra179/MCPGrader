"""Build a stable Worker completion payload while retaining full local results."""

from copy import deepcopy
import json
import math
from typing import Any

from aggregation import AGG_NAME_TO_CLASS


MAX_PUBLICATION_BYTES = 1024 * 1024


class PublicationPayloadError(ValueError):
    """Publication needs intervention; the full result must remain locally durable."""


def serialize_payload(payload: dict[str, Any]) -> bytes:
    """Same encoding as the Worker client; measure signed and transmitted bytes."""
    try:
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise PublicationPayloadError("Result cannot be serialized as finite UTF-8 JSON") from exc


def build_finish_payload(
    submission_id: str,
    task_name: str,
    run_id: str,
    result: dict[str, Any],
    reduction: str = "mean",
    max_bytes: int = MAX_PUBLICATION_BYTES,
) -> dict[str, Any]:
    """Return an independent immutable-by-convention payload to persist before sending.

    Only stdout may be shortened. Other oversized or invalid fields leave publication
    blocked rather than silently losing result data. Reuse this payload on every retry.
    """
    reduction = reduction or "mean"
    if reduction not in AGG_NAME_TO_CLASS:
        raise PublicationPayloadError(f"Unknown reduction: {reduction}")
    runtimes = result.get("runtimes", [])
    if not isinstance(runtimes, list) or any(
        isinstance(value, bool) or not isinstance(value, (int, float))
        or not math.isfinite(value) or value < 0 for value in runtimes
    ):
        raise PublicationPayloadError("Runtimes must be finite nonnegative numbers")
    stdout = result.get("stdout", "")
    error = result.get("error", "")
    data = result.get("data")
    if not isinstance(stdout, str) or not isinstance(error, str):
        raise PublicationPayloadError("stdout and error must be strings")
    if data is not None and not isinstance(data, dict):
        raise PublicationPayloadError("Result data must be a JSON object")
    if result.get("status") not in {"graded", "error", "timeout"}:
        raise PublicationPayloadError("Unknown result status")
    try:
        original_bytes = len(stdout.encode("utf-8"))
    except UnicodeError as exc:
        raise PublicationPayloadError("stdout is not valid UTF-8") from exc
    grading = {
        "commit_hash": result["commit_hash"],
        "status": result["status"],
        "error": error,
        "stdout": stdout,
        "runtimes": deepcopy(runtimes),
        "avg_runtime": AGG_NAME_TO_CLASS[reduction]().aggregate(runtimes),
        "data": deepcopy(data) if data is not None else {},
        "reduction": reduction,
    }
    payload = {"id": submission_id, "task": task_name, "run_id": run_id, "grading": grading}
    if len(serialize_payload(payload)) <= max_bytes:
        return payload
    grading.update(stdout="", stdout_truncated=True, stdout_original_bytes=original_bytes)
    if not stdout or len(serialize_payload(payload)) > max_bytes:
        raise PublicationPayloadError("Result exceeds Worker body limit even without stdout")
    # JSON escaping affects size: find the longest prefix by measuring the whole body.
    lower, upper = 0, len(stdout) - 1
    while lower < upper:
        middle = (lower + upper + 1) // 2
        grading["stdout"] = stdout[:middle]
        if len(serialize_payload(payload)) <= max_bytes:
            lower = middle
        else:
            upper = middle - 1
    grading["stdout"] = stdout[:lower]
    return payload
