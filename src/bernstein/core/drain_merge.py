"""Backward-compatibility shim — moved to bernstein.core.orchestration.drain_merge."""

from bernstein.core._shim import install_shim

install_shim(__name__, "bernstein.core.orchestration.drain_merge")
