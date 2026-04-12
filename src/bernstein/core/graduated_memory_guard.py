"""Backward-compatibility shim — moved to bernstein.core.knowledge.graduated_memory_guard."""

from bernstein.core._shim import install_shim

install_shim(__name__, "bernstein.core.knowledge.graduated_memory_guard")
