"""
blast-radius: AST call graph analyzer for Python projects.

Answers: "if I change file X, what else breaks?"
Stored in SQLite (incremental via SHA-256 — only changed files are re-indexed).

Features:
  - Rich colored terminal tree output (degrades gracefully without Rich)
  - Mermaid diagram output (renders natively in GitHub READMEs / PRs)
  - Watch mode (watchdog or polling fallback)
  - Graphify enrichment (god-node detection when graphify-out/graph.json present)
  - Fully configurable paths — no hardcoded assumptions about your project layout

Quickstart:
    pip install blast-radius
    blast-radius --build .
    blast-radius --query src/utils.py
    blast-radius --query src/utils.py --mermaid
"""
import ast
import datetime
import hashlib
import json
import sqlite3
import sys
import time
from pathlib import Path
from typing import Optional

# ── Defaults (all overridable via CLI flags or API params) ──────────────────
_CWD            = Path.cwd()
DB_PATH         = _CWD / ".blast-radius.db"
GRAPHIFY_GRAPH  = _CWD / "graphify-out" / "graph.json"
SCAN_DIRS       = ["."]
GOD_NODE_THRESHOLD = 30  # in-degree sum >= this → god node

# ── Optional Rich import ─────────────────────────────────────────────────────
try:
    from rich.console import Console
    from rich.tree import Tree
    _RICH = True
    _console = Console()

    def _cprint(msg: str) -> None:
        _console.print(msg)  # type: ignore[union-attr]
except ImportError:
    _RICH = False
    _console = None

    def _cprint(msg: str) -> None:  # type: ignore[misc]
        import re
        print(re.sub(r"\[/?[^\]]*\]", "", msg))


# ─────────────────────────────────────────────────────────────────────────────
# Graphify enrichment layer — optional (needs graphify-out/graph.json)
# ─────────────────────────────────────────────────────────────────────────────

_graphify_cache: dict | None = None


def _load_graphify_graph(graphify_path: Optional[Path] = None) -> dict | None:
    global _graphify_cache
    if _graphify_cache is not None:
        return _graphify_cache
    target = graphify_path or GRAPHIFY_GRAPH
    if not target.exists():
        return None
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
        nodes = data.get("nodes", [])
        edges = data.get("links", data.get("edges", []))
        in_degree: dict[str, int] = {}
        for e in edges:
            tgt = e.get("target")
            if tgt:
                in_degree[tgt] = in_degree.get(tgt, 0) + 1
        file_index: dict[str, dict] = {}
        for n in nodes:
            sf = str(n.get("source_file", "")).replace("\\", "/")
            nid = n.get("id", "")
            deg = in_degree.get(nid, 0)
            if sf not in file_index:
                file_index[sf] = {"total": 0, "nodes": []}
            file_index[sf]["total"] += deg
            if deg > 0:
                file_index[sf]["nodes"].append({"id": nid, "label": n.get("label", nid), "in_degree": deg})
        _graphify_cache = {"file_index": file_index}
        return _graphify_cache
    except Exception:
        return None


def get_graphify_centrality(file_path: str, graphify_path: Optional[Path] = None) -> dict:
    """Return Graphify centrality data for a file (god-node detection).

    Returns ``graphify_available=False`` when graphify-out/graph.json is absent.
    Run ``graphify update .`` in your repo root to generate it.
    """
    g = _load_graphify_graph(graphify_path)
    if g is None:
        return {"file": file_path, "graphify_available": False}
    rel = file_path.replace("\\", "/").lstrip("/")
    file_index = g["file_index"]
    entry = file_index.get(rel)
    if entry is None:
        for k, v in file_index.items():
            if k.endswith(rel) or rel.endswith(k.split("/")[-1]):
                entry = v
                break
    if entry is None:
        return {"file": rel, "in_degree_sum": 0, "top_nodes": [], "god_node": False,
                "graphify_available": True}
    top_nodes = sorted(entry["nodes"], key=lambda x: x["in_degree"], reverse=True)[:5]
    return {
        "file": rel,
        "in_degree_sum": entry["total"],
        "top_nodes": top_nodes,
        "god_node": entry["total"] >= GOD_NODE_THRESHOLD,
        "graphify_available": True,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Internal helpers
# ─────────────────────────────────────────────────────────────────────────────

def _sha(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()[:16]
    except Exception:
        return ""


def _get_db(db_path: Optional[Path] = None) -> sqlite3.Connection:
    target = db_path or DB_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(target))
    conn.executescript("""
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS files (
            path         TEXT PRIMARY KEY,
            sha256       TEXT,
            last_indexed REAL
        );
        CREATE TABLE IF NOT EXISTS imports (
            from_file TEXT,
            to_module TEXT,
            UNIQUE(from_file, to_module)
        );
        CREATE INDEX IF NOT EXISTS idx_imports_to_module ON imports(to_module);
        CREATE TABLE IF NOT EXISTS symbols (
            file_path TEXT,
            name      TEXT,
            kind      TEXT,
            UNIQUE(file_path, name, kind)
        );
    """)
    return conn


# ─────────────────────────────────────────────────────────────────────────────
# Graph build
# ─────────────────────────────────────────────────────────────────────────────

def build_graph(
    force: bool = False,
    scan_dirs: Optional[list[str]] = None,
    repo: Optional[Path] = None,
    db_path: Optional[Path] = None,
) -> int:
    """Build (or incrementally update) the AST import graph.

    Scans ``scan_dirs`` for Python files, extracts imports and symbol names,
    stores them in SQLite. Re-indexes only files whose SHA-256 changed.

    Args:
        force:     Force full rebuild, ignoring SHA cache.
        scan_dirs: Directories to scan (relative to ``repo`` or cwd).
                   Defaults to ["."] (everything under cwd).
        repo:      Root directory.  Defaults to cwd.
        db_path:   SQLite path.    Defaults to ./.blast-radius.db.

    Returns:
        Number of files indexed (0 on a fully warm cache).
    """
    root    = repo or _CWD
    dirs    = scan_dirs or SCAN_DIRS
    conn    = _get_db(db_path)
    cur     = conn.cursor()
    indexed = 0

    for scan_dir in dirs:
        scan_path = root / scan_dir
        if not scan_path.exists():
            continue
        for path in scan_path.rglob("*.py"):
            if "__pycache__" in str(path):
                continue
            try:
                rel = str(path.relative_to(root)).replace("\\", "/")
            except ValueError:
                rel = str(path).replace("\\", "/")
            sha = _sha(path)

            if not force:
                row = cur.execute("SELECT sha256 FROM files WHERE path=?", (rel,)).fetchone()
                if row and row[0] == sha:
                    continue

            try:
                tree = ast.parse(path.read_text(encoding="utf-8", errors="ignore"))
            except Exception:
                continue

            cur.execute("DELETE FROM imports WHERE from_file=?",  (rel,))
            cur.execute("DELETE FROM symbols WHERE file_path=?", (rel,))

            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        cur.execute("INSERT OR IGNORE INTO imports VALUES (?,?)", (rel, alias.name))
                elif isinstance(node, ast.ImportFrom):
                    if node.module:
                        cur.execute("INSERT OR IGNORE INTO imports VALUES (?,?)", (rel, node.module))
                elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    cur.execute("INSERT OR IGNORE INTO symbols VALUES (?,?,?)", (rel, node.name, "function"))
                elif isinstance(node, ast.ClassDef):
                    cur.execute("INSERT OR IGNORE INTO symbols VALUES (?,?,?)", (rel, node.name, "class"))

            cur.execute("INSERT OR REPLACE INTO files VALUES (?,?,?)", (rel, sha, time.time()))
            indexed += 1

    conn.commit()
    conn.close()
    return indexed


# ─────────────────────────────────────────────────────────────────────────────
# Blast radius query
# ─────────────────────────────────────────────────────────────────────────────

def get_blast_radius(
    file_path: str,
    db_path: Optional[Path] = None,
    graphify_path: Optional[Path] = None,
) -> dict:
    """Return the blast radius for a given source file.

    Example::

        from blast_radius import build_graph, get_blast_radius
        build_graph()
        result = get_blast_radius("src/utils.py")
        # {"file": "src/utils.py", "direct_dependents": [...], "total_affected": 7}

    Args:
        file_path:     Path to the file (relative to project root, or absolute).
        db_path:       SQLite path.  Defaults to ./.blast-radius.db.
        graphify_path: Path to graphify-out/graph.json (optional enrichment).

    Returns:
        Dict with ``file``, ``direct_dependents``, ``test_files``,
        ``total_affected``, and optionally ``graphify`` metadata.
    """
    conn = _get_db(db_path)
    cur  = conn.cursor()
    rel  = file_path.replace("\\", "/").lstrip("/")
    mod  = rel.replace("/", ".").removesuffix(".py")

    file_count = cur.execute("SELECT COUNT(*) FROM files").fetchone()[0]
    if file_count == 0:
        conn.close()
        return {
            "file":              rel,
            "direct_dependents": [],
            "test_files":        [],
            "total_affected":    0,
            "warning":           "Graph is empty — run build_graph() first.",
        }

    dependents = cur.execute(
        "SELECT DISTINCT from_file FROM imports WHERE to_module=?", (mod,)
    ).fetchall()

    all_files  = [r[0] for r in dependents if r[0] != rel]
    test_files = [f for f in all_files if f.startswith("tests/") or "/test_" in f]
    conn.close()

    result = {
        "file":              rel,
        "direct_dependents": all_files,
        "test_files":        test_files,
        "total_affected":    len(all_files),
    }
    centrality = get_graphify_centrality(rel, graphify_path)
    if centrality.get("graphify_available"):
        result["graphify"] = {
            "in_degree_sum": centrality["in_degree_sum"],
            "god_node":      centrality["god_node"],
            "top_nodes":     centrality.get("top_nodes", []),
        }
    return result


# ─────────────────────────────────────────────────────────────────────────────
# Mermaid diagram output
# ─────────────────────────────────────────────────────────────────────────────

def to_mermaid(result: dict) -> str:
    """Convert a get_blast_radius() result to a Mermaid flowchart string.

    Renders natively in GitHub READMEs, issues, and PR descriptions.
    God nodes are highlighted in red; test files in green.

    Example::

        result = get_blast_radius("src/utils.py")
        print(to_mermaid(result))
        # graph TD
        #     utils_py["src/utils.py"]
        #     utils_py --> other_py["src/other.py"]
        #     utils_py --> test_utils_py["tests/test_utils.py"]
        #     style test_utils_py fill:#22c55e,color:#fff
    """
    target = result["file"].split("/")[-1].replace(".", "_")
    lines  = ["graph TD"]
    god    = result.get("graphify", {}).get("god_node", False)

    if god:
        lines.append(f'    {target}["\U0001f534 {result["file"]} — GOD NODE"]')
        lines.append(f"    style {target} fill:#ff4444,color:#fff,stroke:#cc0000")
    else:
        lines.append(f'    {target}["{result["file"]}"]')

    if not result["direct_dependents"]:
        lines.append(f"    {target} --> NONE[no dependents]")
        lines.append("    style NONE fill:#888,color:#fff")
        return "\n".join(lines)

    for dep in result["direct_dependents"]:
        dep_id    = dep.split("/")[-1].replace(".", "_").replace("-", "_")
        is_test   = dep in result.get("test_files", [])
        lines.append(f'    {target} --> {dep_id}["{dep}"]')
        if is_test:
            lines.append(f"    style {dep_id} fill:#22c55e,color:#fff")

    in_deg = result.get("graphify", {}).get("in_degree_sum", 0)
    lines.append(f'    %% total affected: {result["total_affected"]} | in-degree: {in_deg}')
    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# Rich terminal output
# ─────────────────────────────────────────────────────────────────────────────

def _print_result_rich(result: dict) -> None:
    if not _RICH:
        _print_result_plain(result)
        return

    god    = result.get("graphify", {}).get("god_node", False)
    in_deg = result.get("graphify", {}).get("in_degree_sum", 0)
    total  = result["total_affected"]

    title_color = "bold red" if god else "bold cyan"
    god_badge   = " [bold red]⚡ GOD NODE[/bold red]" if god else ""
    title = f"[{title_color}]{result['file']}[/{title_color}]{god_badge}"

    rich_tree = Tree(title)  # type: ignore[operator]

    if result.get("warning"):
        rich_tree.add(f"[yellow]⚠ {result['warning']}[/yellow]")
    elif not result["direct_dependents"]:
        rich_tree.add("[dim]No dependents found[/dim]")
    else:
        for dep in result["direct_dependents"]:
            is_test = dep in result.get("test_files", [])
            color   = "green" if is_test else "white"
            prefix  = "🧪 " if is_test else "📄 "
            rich_tree.add(f"[{color}]{prefix}{dep}[/{color}]")

    summary_parts = [f"[bold]{total}[/bold] file(s) affected"]
    if in_deg:
        summary_parts.append(f"in-degree [bold]{in_deg}[/bold]")
    if result.get("test_files"):
        summary_parts.append(f"[green]{len(result['test_files'])} test file(s)[/green]")

    _cprint(str(rich_tree))
    _cprint("  " + " · ".join(summary_parts))


def _print_result_plain(result: dict) -> None:
    god   = result.get("graphify", {}).get("god_node", False)
    total = result["total_affected"]
    print(f"\nBlast radius: {result['file']}" + (" [GOD NODE]" if god else ""))
    print(f"Total affected: {total}")
    for dep in result["direct_dependents"]:
        tag = " [TEST]" if dep in result.get("test_files", []) else ""
        print(f"  → {dep}{tag}")
    if result.get("warning"):
        print(f"WARNING: {result['warning']}")


# ─────────────────────────────────────────────────────────────────────────────
# Watch mode
# ─────────────────────────────────────────────────────────────────────────────

def watch(
    scan_dirs: Optional[list[str]] = None,
    repo: Optional[Path] = None,
    db_path: Optional[Path] = None,
    interval: float = 2.0,
) -> None:
    """Monitor for file changes and auto-rebuild the graph.

    Uses watchdog if installed (inotify/FSEvents/kqueue), otherwise falls back
    to polling via stat() — no extra dependency required for basic use.

    Args:
        scan_dirs: Directories to watch.  Defaults to ["."].
        repo:      Root directory.        Defaults to cwd.
        db_path:   SQLite path.           Defaults to ./.blast-radius.db.
        interval:  Poll interval (seconds). Used by both watchdog and polling.

    Runs until Ctrl+C.
    """
    root = repo or _CWD
    dirs = scan_dirs or SCAN_DIRS

    try:
        from watchdog.observers import Observer
        from watchdog.events import FileSystemEventHandler

        class _Handler(FileSystemEventHandler):
            def __init__(self):
                self.dirty = False
            def on_any_event(self, event):
                if not event.is_directory and str(event.src_path).endswith(".py"):
                    self.dirty = True

        handler  = _Handler()
        observer = Observer()
        for d in dirs:
            p = root / d
            if p.exists():
                observer.schedule(handler, str(p), recursive=True)
        observer.start()
        _cprint("[bold cyan]Watching for changes (watchdog). Press Ctrl+C to stop.[/bold cyan]")

        try:
            while True:
                time.sleep(interval)
                if handler.dirty:
                    handler.dirty = False
                    n  = build_graph(scan_dirs=dirs, repo=root, db_path=db_path)
                    ts = datetime.datetime.now().strftime("%H:%M:%S")
                    _cprint(f"[green][{ts}] Rebuilt — {n} file(s) re-indexed[/green]")
        except KeyboardInterrupt:
            observer.stop()
        observer.join()

    except ImportError:
        _cprint(f"[bold cyan]Watching for changes (polling every {interval}s). Press Ctrl+C to stop.[/bold cyan]")

        def _snapshot(dirs, root):
            s = {}
            for d in dirs:
                for p in (root / d).rglob("*.py"):
                    if "__pycache__" not in str(p):
                        try:
                            s[str(p)] = p.stat().st_mtime
                        except Exception:
                            pass
            return s

        prev = _snapshot(dirs, root)
        try:
            while True:
                time.sleep(interval)
                curr = _snapshot(dirs, root)
                if curr != prev:
                    prev = curr
                    n  = build_graph(scan_dirs=dirs, repo=root, db_path=db_path)
                    ts = datetime.datetime.now().strftime("%H:%M:%S")
                    _cprint(f"[green][{ts}] Rebuilt — {n} file(s) re-indexed[/green]")
        except KeyboardInterrupt:
            pass

    print("\nWatch stopped.")


# ─────────────────────────────────────────────────────────────────────────────
# Staleness check
# ─────────────────────────────────────────────────────────────────────────────

def rebuild_if_stale(
    max_age_seconds: int = 300,
    scan_dirs: Optional[list[str]] = None,
    repo: Optional[Path] = None,
    db_path: Optional[Path] = None,
) -> bool:
    """Rebuild the graph if it is older than ``max_age_seconds``.

    Returns True if a rebuild was triggered.
    """
    target = db_path or DB_PATH
    try:
        age = time.time() - target.stat().st_mtime
    except FileNotFoundError:
        build_graph(scan_dirs=scan_dirs, repo=repo, db_path=db_path)
        return True
    if age > max_age_seconds:
        build_graph(scan_dirs=scan_dirs, repo=repo, db_path=db_path)
        return True
    return False


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

def _cli() -> None:
    import argparse

    p = argparse.ArgumentParser(
        prog="blast-radius",
        description="AST call graph analyzer — 'what breaks if I change this file?'"
    )
    p.add_argument("--build",    action="store_true", help="(Re)build the code graph.")
    p.add_argument("--force",    action="store_true", help="Force full rebuild (ignore SHA cache).")
    p.add_argument("--query",    default="",          help="Show blast radius for FILE.")
    p.add_argument("--mermaid",  action="store_true", help="Output Mermaid diagram (use with --query).")
    p.add_argument("--watch",    action="store_true", help="Watch for changes and auto-rebuild.")
    p.add_argument("--json",     action="store_true", help="Output raw JSON.")
    p.add_argument("--scan",     nargs="+",           help="Directories to scan (default: current directory).")
    p.add_argument("--db",       default="",          help="Path to SQLite DB (default: ./.blast-radius.db).")
    p.add_argument("--interval", type=float, default=2.0, help="Watch poll interval in seconds (default: 2.0).")
    args = p.parse_args()

    scan = args.scan or None
    db   = Path(args.db) if args.db else None

    if args.watch:
        watch(scan_dirs=scan, db_path=db, interval=args.interval)
        sys.exit(0)

    if args.build:
        n = build_graph(force=args.force, scan_dirs=scan, db_path=db)
        print(f"Built graph — {n} file(s) indexed.")

    if args.query:
        result = get_blast_radius(args.query, db_path=db)
        if args.json:
            print(json.dumps(result, indent=2))
        elif args.mermaid:
            print("```mermaid")
            print(to_mermaid(result))
            print("```")
        else:
            _print_result_rich(result)

    if not any([args.build, args.query, args.watch]):
        p.print_help()


if __name__ == "__main__":
    _cli()
