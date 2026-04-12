"""Backward-compatibility shim — moved to bernstein.core.quality.release_train."""

from bernstein.core._shim import install_shim

install_shim(__name__, "bernstein.core.quality.release_train")
