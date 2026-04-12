"""Backward-compat shim: module moved to bernstein.core.quality.dead_code_detector."""

from bernstein.core._shim import install_shim

install_shim(__name__, "bernstein.core.quality.dead_code_detector")
