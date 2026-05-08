"""
blast-radius pytest plugin.

When --blast-radius (or --br) is passed to pytest, only tests whose file
path appears in the blast radius of the staged (or specified) source files
are collected and run.  Everything else is deselected.

Registration (automatic after install):
    pip install "impact-radius[pytest]"

    # pytest.ini or pyproject.toml:
    [pytest]
    addopts = --blast-radius          # always targeted
    # OR run ad-hoc:
    pytest --blast-radius

Usage examples:
    pytest --blast-radius                    # staged files (pre-commit mode)
    pytest --blast-radius --br-file src/foo.py   # explicit file
    pytest --blast-radius --br-all           # bypass (run full suite)
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import List, Set

import pytest


def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup("blast-radius", "Blast Radius targeted test selection")
    group.addoption(
        "--blast-radius",
        "--br",
        action="store_true",
        default=False,
        help="Run only tests in the blast radius of staged (or specified) source files.",
    )
    group.addoption(
        "--br-file",
        action="append",
        default=[],
        metavar="FILE",
        help="Source file to analyze (default: git staged .py files). Repeatable.",
    )
    group.addoption(
        "--br-all",
        action="store_true",
        default=False,
        help="Bypass blast radius filter — run full suite (useful with --blast-radius in addopts).",
    )


def _staged_py_files() -> List[str]:
    try:
        result = subprocess.run(
            ["git", "diff", "--cached", "--name-only", "--diff-filter=ACMR"],
            capture_output=True, text=True, timeout=5,
        )
        return [f for f in result.stdout.splitlines() if f.endswith(".py")]
    except Exception:
        return []


def _affected_tests(source_files: List[str]) -> Set[str]:
    try:
        from blast_radius.core import get_blast_radius, rebuild_if_stale
        rebuild_if_stale()
    except Exception:
        return set()

    affected: Set[str] = set()
    for src in source_files:
        src_norm = src.replace("\\", "/")
        try:
            result = get_blast_radius(src_norm)
            for tf in result.get("test_files", []):
                if Path(tf).exists():
                    affected.add(tf)
        except Exception:
            pass

        # Convention-based fallback: tests/test_<stem>.py
        stem = Path(src).stem
        candidate = f"tests/test_{stem}.py"
        if Path(candidate).exists():
            affected.add(candidate)

    return affected


def pytest_collection_modifyitems(
    config: pytest.Config,
    items: List[pytest.Item],
) -> None:
    if not config.getoption("--blast-radius", default=False):
        return
    if config.getoption("--br-all", default=False):
        return

    explicit = config.getoption("--br-file", default=[])
    source_files = list(explicit) if explicit else _staged_py_files()

    if not source_files:
        return  # nothing staged → run everything (safe fallback)

    # Split: test files staged directly vs. source files needing graph lookup
    direct_tests = {f for f in source_files if "test_" in Path(f).name or "tests" in Path(f).parts}
    src_only = [f for f in source_files if f not in direct_tests]

    affected = set(direct_tests)
    if src_only:
        affected |= _affected_tests(src_only)

    if not affected:
        return  # no tests resolved → run everything (safe fallback)

    # Resolve to absolute paths for comparison
    affected_abs = set()
    for f in affected:
        p = Path(f)
        if p.exists():
            affected_abs.add(p.resolve())

    selected = []
    deselected = []
    for item in items:
        if Path(item.fspath).resolve() in affected_abs:
            selected.append(item)
        else:
            deselected.append(item)

    if deselected:
        config.hook.pytest_deselected(items=deselected)
        items[:] = selected
