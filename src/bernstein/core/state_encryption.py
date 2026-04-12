"""Backward-compat shim: re-exports from bernstein.core.security.state_encryption."""

from bernstein.core._shim import install_shim

install_shim(__name__, "bernstein.core.security.state_encryption")
