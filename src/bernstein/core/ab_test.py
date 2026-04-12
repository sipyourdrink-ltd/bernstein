"""Backward-compatibility shim — moved to bernstein.core.quality.ab_test."""

from bernstein.core._shim import install_shim

install_shim(__name__, "bernstein.core.quality.ab_test")
