"""Backward-compat shim: re-exports from bernstein.core.security.auth_rate_limiter."""

from bernstein.core._shim import install_shim

install_shim(__name__, "bernstein.core.security.auth_rate_limiter")
