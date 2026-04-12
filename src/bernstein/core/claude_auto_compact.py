"""Backward-compat shim for bernstein.core.tokens.claude_auto_compact."""

from bernstein.core._shim import install_shim

install_shim(__name__, "bernstein.core.tokens.claude_auto_compact")
