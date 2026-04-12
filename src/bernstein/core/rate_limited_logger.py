"""Backward-compat shim — re-exports from bernstein.core.observability.rate_limited_logger."""

from bernstein.core._shim import install_shim

install_shim(__name__, "bernstein.core.observability.rate_limited_logger")
