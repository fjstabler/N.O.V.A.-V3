#!/usr/bin/env python3
"""Verify the Python and TypeScript protocol definitions agree.

The wire protocol is declared twice — once in `nova/transport/protocol.py` and
once in `packages/protocol/src/index.ts` — because neither side should have to
import the other's toolchain. Two declarations means they can drift, and the
failure mode is silent: a request routes nowhere, or an event nobody handles.

This script makes that drift a build failure instead. Run it in CI.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PYTHON_PROTOCOL = ROOT / "services" / "core" / "nova" / "transport" / "protocol.py"
PYTHON_EVENTS = ROOT / "services" / "core" / "nova" / "runtime" / "events.py"
TYPESCRIPT = ROOT / "packages" / "protocol" / "src" / "index.ts"

#: Topics the core publishes internally but deliberately never forwards.
INTERNAL_ONLY: frozenset[str] = frozenset()


def python_string_constants(path: Path, class_name: str) -> set[str]:
    """Extract `NAME = "value"` entries from a class body."""
    source = path.read_text(encoding="utf-8")
    match = re.search(rf"^class {class_name}\b.*?:\n(.*?)(?=\n\S|\Z)", source, re.S | re.M)
    if not match:
        raise SystemExit(f"could not find class {class_name} in {path}")
    return set(re.findall(r'^\s+[A-Z_]+\s*=\s*"([^"]+)"', match.group(1), re.M))


def typescript_string_constants(path: Path, const_name: str) -> set[str]:
    """Extract `Key: 'value'` entries from an exported const object."""
    source = path.read_text(encoding="utf-8")
    match = re.search(rf"export const {const_name} = \{{(.*?)\n\}} as const;", source, re.S)
    if not match:
        raise SystemExit(f"could not find const {const_name} in {path}")
    return set(re.findall(r"^\s+\w+:\s*'([^']+)'", match.group(1), re.M))


def protocol_version(python: Path, typescript: Path) -> tuple[int, int]:
    py = re.search(r"^PROTOCOL_VERSION = (\d+)", python.read_text(encoding="utf-8"), re.M)
    ts = re.search(r"^export const PROTOCOL_VERSION = (\d+);", typescript.read_text(encoding="utf-8"), re.M)
    if not py or not ts:
        raise SystemExit("could not read PROTOCOL_VERSION from both sides")
    return int(py.group(1)), int(ts.group(1))


def compare(label: str, python: set[str], typescript: set[str]) -> list[str]:
    problems = []
    for missing in sorted(python - typescript):
        problems.append(f"  {label}: '{missing}' exists in Python but not in TypeScript")
    for missing in sorted(typescript - python):
        problems.append(f"  {label}: '{missing}' exists in TypeScript but not in Python")
    return problems


def main() -> int:
    problems: list[str] = []

    py_version, ts_version = protocol_version(PYTHON_PROTOCOL, TYPESCRIPT)
    if py_version != ts_version:
        problems.append(f"  PROTOCOL_VERSION differs: Python {py_version}, TypeScript {ts_version}")

    problems += compare(
        "request",
        python_string_constants(PYTHON_PROTOCOL, "Requests"),
        typescript_string_constants(TYPESCRIPT, "Requests"),
    )
    problems += compare(
        "topic",
        python_string_constants(PYTHON_EVENTS, "Topics") - INTERNAL_ONLY,
        typescript_string_constants(TYPESCRIPT, "Topics"),
    )

    if problems:
        print("Protocol definitions have drifted:\n", file=sys.stderr)
        print("\n".join(problems), file=sys.stderr)
        print(
            f"\nUpdate {TYPESCRIPT.relative_to(ROOT)} or the Python definitions so they match.",
            file=sys.stderr,
        )
        return 1

    print("Protocol definitions agree.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
