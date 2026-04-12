"""Backward-compatibility shim — moved to bernstein.core.git.pr_size_governor."""

from bernstein.core._shim import install_shim

install_shim(__name__, "bernstein.core.git.pr_size_governor")
