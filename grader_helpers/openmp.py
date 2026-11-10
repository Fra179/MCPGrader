"""Inspect binaries for evidence of OpenMP linkage."""

import re
import subprocess
from pathlib import Path


_RUNTIME = re.compile(
    r"\blib(?:gomp|omp|iomp5|nvomp)\.(?:so(?:\.[\w.-]+)?|a)\b"
)
_SYMBOL = re.compile(r"^(?:GOMP_|gomp_|omp_|__kmpc_|__kmp_)")


def check_openmp(binary, *, link_map=None, use_ldd=False, timeout=10):
    """Checks for evidence of OpenMP linkage in a binary. Both static and dynamic.
    """
    binary = Path(binary).expanduser().resolve(strict=True)
    map_path = (
        Path(link_map).expanduser().resolve(strict=True)
        if link_map is not None else None
    )
    result = {
        "binary": str(binary),
        "present": None,
        "linkage": [],
        "evidence": [],
        "checks": {},
        "limitations": [
            "No matches do not prove absence: symbols may be stripped or "
            "renamed, optimized away, or belong to an unrecognized runtime.",
            "Dependencies loaded later at runtime are not covered.",
            "Linked runtime code does not prove OpenMP regions are executed.",
        ],
    }

    def run(name, args):
        try:
            proc = subprocess.run(
                args, capture_output=True, text=True, errors="replace",
                timeout=timeout, check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            result["checks"][name] = {"ok": False, "diagnostic": str(exc)}
            return ""
        result["checks"][name] = {
            "ok": proc.returncode == 0,
            "returncode": proc.returncode,
            "diagnostic": proc.stderr.strip(),
        }
        return proc.stdout

    def record(kind, source, lines):
        if lines:
            if kind not in result["linkage"]:
                result["linkage"].append(kind)
            result["evidence"].append({"source": source, "matches": lines})
            if kind != "map_input":
                result["present"] = True

    dynamic = run("readelf", ["readelf", "-d", str(binary)])
    record("dynamic", "readelf: direct shared dependencies", [
        line.strip() for line in dynamic.splitlines()
        if "(NEEDED)" in line and _RUNTIME.search(line)
    ])

    symbols = run("nm", ["nm", "-a", str(binary)])
    defined, undefined = [], []
    for line in symbols.splitlines():
        fields = line.split()
        if len(fields) < 2:
            continue
        symbol = fields[-1].split("@", 1)[0]
        symbol_type = fields[-2]
        if not _SYMBOL.match(symbol):
            continue
        if symbol_type in {"U", "w", "v"}:
            undefined.append(line.strip())
        elif len(fields) >= 3:
            defined.append(line.strip())
    record("static_or_embedded", "nm: defined OpenMP symbols", defined)
    # References alone do not establish which library provides the symbols.
    if undefined:
        result["evidence"].append({
            "source": "nm: unresolved OpenMP symbol references",
            "matches": undefined,
        })

    if use_ldd:
        dependencies = run("ldd", ["ldd", str(binary)])
        record("dynamic", "ldd: shared dependency tree", [
            line.strip() for line in dependencies.splitlines()
            if _RUNTIME.search(line)
        ])

    if map_path is not None:
        map_text = map_path.read_text(errors="replace")
        record("map_input", "linker map: static runtime inputs", [
            line.strip() for line in map_text.splitlines()
            if _RUNTIME.search(line) and ".a(" in line
        ])
        result["checks"]["link_map"] = {"ok": True}
        result["limitations"].append(
            "A map match identifies an archive input; verify its sections "
            "were retained and that the map belongs to this binary."
        )

    return result
