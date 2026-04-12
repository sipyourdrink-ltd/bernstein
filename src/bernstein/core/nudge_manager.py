"""Backward-compatibility shim — moved to bernstein.core.orchestration.nudge_manager."""

from bernstein.core._shim import install_shim

install_shim(__name__, "bernstein.core.orchestration.nudge_manager")
