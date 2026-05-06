"""blast-radius: AST call graph analyzer for Python projects.

Answers the question: "if I change this file, what else breaks?"

>>> from blast_radius import build_graph, get_blast_radius, to_mermaid
>>> build_graph()
>>> result = get_blast_radius("src/utils.py")
>>> print(to_mermaid(result))
"""
from .core import (
    build_graph,
    get_blast_radius,
    get_graphify_centrality,
    to_mermaid,
    watch,
    rebuild_if_stale,
)

__all__ = [
    "build_graph",
    "get_blast_radius",
    "get_graphify_centrality",
    "to_mermaid",
    "watch",
    "rebuild_if_stale",
]

__version__ = "1.0.0"
