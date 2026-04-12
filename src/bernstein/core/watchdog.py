"""Backward-compat shim — re-exports from bernstein.core.observability.watchdog."""

from bernstein.core._shim import install_shim

install_shim(__name__, "bernstein.core.observability.watchdog")
