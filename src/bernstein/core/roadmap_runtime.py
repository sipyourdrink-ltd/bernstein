"""Backward-compatibility shim — moved to bernstein.core.planning.roadmap_runtime."""

from bernstein.core._shim import install_shim

install_shim(__name__, "bernstein.core.planning.roadmap_runtime")
