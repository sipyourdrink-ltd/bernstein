"""Backward-compat shim: re-exports from bernstein.core.security.permission_rules."""

from bernstein.core._shim import install_shim

install_shim(__name__, "bernstein.core.security.permission_rules")
