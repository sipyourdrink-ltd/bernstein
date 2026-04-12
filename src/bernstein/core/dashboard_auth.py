"""Backward-compatibility shim — moved to bernstein.core.server.dashboard_auth."""

from bernstein.core._shim import install_shim

install_shim(__name__, "bernstein.core.server.dashboard_auth")
