"""Backward-compatibility shim — moved to bernstein.core.orchestration.evolve_mode."""

from bernstein.core._shim import install_shim

install_shim(__name__, "bernstein.core.orchestration.evolve_mode")
