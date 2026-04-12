"""Backward-compatibility shim — moved to bernstein.core.git.incremental_merge."""

from bernstein.core._shim import install_shim

install_shim(__name__, "bernstein.core.git.incremental_merge")
