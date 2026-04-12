"""Backward-compat shim for bernstein.core.tokens.auto_compact."""

from bernstein.core._shim import install_shim

install_shim(__name__, "bernstein.core.tokens.auto_compact")
