"""Backward-compat shim: re-exports from bernstein.core.security.blocking_hooks."""

from bernstein.core._shim import install_shim

install_shim(__name__, "bernstein.core.security.blocking_hooks")
