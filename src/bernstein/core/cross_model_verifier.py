"""Backward-compat shim: module moved to bernstein.core.quality.cross_model_verifier."""

from bernstein.core._shim import install_shim

install_shim(__name__, "bernstein.core.quality.cross_model_verifier")
