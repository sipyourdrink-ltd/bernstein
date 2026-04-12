"""Backward-compatibility shim — moved to bernstein.core.git.worktree."""

from bernstein.core._shim import install_shim

install_shim(__name__, "bernstein.core.git.worktree")
