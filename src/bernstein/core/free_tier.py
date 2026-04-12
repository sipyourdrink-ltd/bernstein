"""Backward-compatibility shim — moved to bernstein.core.cost.free_tier."""

from bernstein.core._shim import install_shim

install_shim(__name__, "bernstein.core.cost.free_tier")
