"""Backward-compatibility shim — moved to bernstein.core.knowledge.memory_lock_protocol."""

from bernstein.core._shim import install_shim

install_shim(__name__, "bernstein.core.knowledge.memory_lock_protocol")
