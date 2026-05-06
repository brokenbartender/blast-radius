"""Tests for blast-radius standalone package."""
import json
import tempfile
import textwrap
from pathlib import Path

import pytest

from blast_radius import build_graph, get_blast_radius, to_mermaid


@pytest.fixture()
def sample_project(tmp_path: Path) -> Path:
    """Create a tiny synthetic Python project."""
    (tmp_path / "src").mkdir()
    (tmp_path / "tests").mkdir()

    (tmp_path / "src" / "utils.py").write_text(
        "def helper(): pass\n", encoding="utf-8"
    )
    (tmp_path / "src" / "main.py").write_text(
        "from src.utils import helper\n", encoding="utf-8"
    )
    (tmp_path / "tests" / "test_utils.py").write_text(
        "from src.utils import helper\n", encoding="utf-8"
    )
    return tmp_path


def test_build_graph(sample_project: Path):
    db = sample_project / ".blast-radius.db"
    n = build_graph(scan_dirs=["."], repo=sample_project, db_path=db)
    assert n >= 2, f"Expected at least 2 files indexed, got {n}"


def test_get_blast_radius(sample_project: Path):
    db = sample_project / ".blast-radius.db"
    build_graph(scan_dirs=["."], repo=sample_project, db_path=db)
    result = get_blast_radius("src/utils.py", db_path=db)
    assert result["total_affected"] >= 1
    assert any("main" in d for d in result["direct_dependents"])


def test_test_files_detected(sample_project: Path):
    db = sample_project / ".blast-radius.db"
    build_graph(scan_dirs=["."], repo=sample_project, db_path=db)
    result = get_blast_radius("src/utils.py", db_path=db)
    assert any("test_utils" in f for f in result["test_files"])


def test_to_mermaid_structure(sample_project: Path):
    db = sample_project / ".blast-radius.db"
    build_graph(scan_dirs=["."], repo=sample_project, db_path=db)
    result = get_blast_radius("src/utils.py", db_path=db)
    diagram = to_mermaid(result)
    assert diagram.startswith("graph TD")
    assert "utils_py" in diagram


def test_to_mermaid_no_deps():
    result = {
        "file": "orphan.py",
        "direct_dependents": [],
        "test_files": [],
        "total_affected": 0,
    }
    diagram = to_mermaid(result)
    assert "NONE" in diagram


def test_empty_graph_warning(tmp_path: Path):
    db = tmp_path / "empty.db"
    result = get_blast_radius("nonexistent.py", db_path=db)
    assert "warning" in result
    assert result["total_affected"] == 0


def test_incremental_build(sample_project: Path):
    db = sample_project / ".blast-radius.db"
    n1 = build_graph(scan_dirs=["."], repo=sample_project, db_path=db)
    n2 = build_graph(scan_dirs=["."], repo=sample_project, db_path=db)
    # Second build should re-index 0 files (nothing changed)
    assert n2 == 0, f"Expected 0 files on warm cache, got {n2}"


def test_force_rebuild(sample_project: Path):
    db = sample_project / ".blast-radius.db"
    build_graph(scan_dirs=["."], repo=sample_project, db_path=db)
    n = build_graph(force=True, scan_dirs=["."], repo=sample_project, db_path=db)
    assert n >= 2
