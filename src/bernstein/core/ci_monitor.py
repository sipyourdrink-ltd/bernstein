"""Backward-compatibility shim — moved to bernstein.core.quality.ci_monitor."""

from bernstein.core._shim import install_shim

install_shim(__name__, "bernstein.core.quality.ci_monitor")
