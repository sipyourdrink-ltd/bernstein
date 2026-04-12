"""Backward-compat shim — re-exports from bernstein.core.observability.custom_metrics."""

from bernstein.core._shim import install_shim

install_shim(__name__, "bernstein.core.observability.custom_metrics")
