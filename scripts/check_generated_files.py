#!/usr/bin/env python3
"""Fail when generated data artifacts are tracked by Git."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


FORBIDDEN_SUFFIXES = {".duckdb", ".duckdb-shm", ".duckdb-wal", ".zip", ".docx", ".parquet"}
FORBIDDEN_NAMES = {
    "cignaPretty.json",
    "in_network_rates_filtered.json",
    "ingestion_runs.jsonl",
    "report.txt",
    "validation_report.txt",
}


def tracked_files() -> list[Path]:
    result = subprocess.run(
        ["git", "ls-files", "-z"],
        check=True,
        capture_output=True,
        text=False,
    )
    return [Path(name) for name in result.stdout.decode().split("\0") if name]


def forbidden_files(paths: list[Path]) -> list[Path]:
    return [
        path
        for path in paths
        if path.name in FORBIDDEN_NAMES or any(path.name.endswith(suffix) for suffix in FORBIDDEN_SUFFIXES)
    ]


def main() -> int:
    violations = forbidden_files(tracked_files())
    if violations:
        print("Generated artifacts must not be tracked:")
        for path in violations:
            print(f"  {path}")
        return 1
    print("No generated data artifacts are tracked.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
