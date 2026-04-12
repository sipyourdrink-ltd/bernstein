"""Backward-compatibility shim — moved to bernstein.core.quality.complexity_advisor."""

from bernstein.core._shim import install_shim

install_shim(__name__, "bernstein.core.quality.complexity_advisor")
