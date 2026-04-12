"""Backward-compatibility shim — moved to bernstein.core.git.git_hygiene."""

from bernstein.core._shim import install_shim

install_shim(__name__, "bernstein.core.git.git_hygiene")
