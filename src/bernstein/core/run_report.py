"""Backward-compatibility shim — moved to bernstein.core.orchestration.run_report."""

from bernstein.core._shim import install_shim

install_shim(__name__, "bernstein.core.orchestration.run_report")
