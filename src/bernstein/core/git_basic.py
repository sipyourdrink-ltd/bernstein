"""Backward-compatibility shim — moved to bernstein.core.git.git_basic."""

from bernstein.core._shim import install_shim

install_shim(__name__, "bernstein.core.git.git_basic")
