"""Backward-compat shim for bernstein.core.tokens.prompt_versioning."""

from bernstein.core._shim import install_shim

install_shim(__name__, "bernstein.core.tokens.prompt_versioning")
