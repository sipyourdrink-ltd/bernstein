"""Backward-compatibility shim — moved to bernstein.core.planning.scenario_library."""

from bernstein.core._shim import install_shim

install_shim(__name__, "bernstein.core.planning.scenario_library")
