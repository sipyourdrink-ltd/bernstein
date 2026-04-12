"""Backward-compatibility shim — moved to bernstein.core.tasks.abort_chain."""

from bernstein.core._shim import install_shim

install_shim(__name__, "bernstein.core.tasks.abort_chain")
