"""Backward-compatibility shim — moved to bernstein.core.orchestration.activity_summary_poller."""

from bernstein.core._shim import install_shim

install_shim(__name__, "bernstein.core.orchestration.activity_summary_poller")
