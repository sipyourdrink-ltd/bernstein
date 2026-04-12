"""Backward-compatibility shim — moved to bernstein.core.orchestration.orchestrator_cleanup."""

from bernstein.core._shim import install_shim

install_shim(__name__, "bernstein.core.orchestration.orchestrator_cleanup")
