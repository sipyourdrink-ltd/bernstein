"""Backward-compatibility shim — moved to bernstein.core.cost.cheaper_retry."""

from bernstein.core._shim import install_shim

install_shim(__name__, "bernstein.core.cost.cheaper_retry")
