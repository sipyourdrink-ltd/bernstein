"""Backward-compatibility shim — moved to bernstein.core.knowledge.knowledge_graph."""

from bernstein.core._shim import install_shim

install_shim(__name__, "bernstein.core.knowledge.knowledge_graph")
