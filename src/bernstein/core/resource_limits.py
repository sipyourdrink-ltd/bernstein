"""Backward-compatibility shim — moved to bernstein.core.security.resource_limits."""

from bernstein.core._shim import install_shim

install_shim(__name__, "bernstein.core.security.resource_limits")
