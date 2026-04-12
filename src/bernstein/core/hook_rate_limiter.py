"""Backward-compatibility shim — moved to bernstein.core.config.hook_rate_limiter."""

from bernstein.core._shim import install_shim

install_shim(__name__, "bernstein.core.config.hook_rate_limiter")
