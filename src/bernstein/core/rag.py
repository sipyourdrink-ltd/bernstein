"""Backward-compatibility shim — moved to bernstein.core.knowledge.rag."""

from bernstein.core._shim import install_shim

install_shim(__name__, "bernstein.core.knowledge.rag")
