"""Backward-compat shim — re-exports from bernstein.core.observability.sla_monitor."""

from bernstein.core._shim import install_shim

install_shim(__name__, "bernstein.core.observability.sla_monitor")
