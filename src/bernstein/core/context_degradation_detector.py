"""Backward-compat shim for bernstein.core.tokens.context_degradation_detector."""

from bernstein.core._shim import install_shim

install_shim(__name__, "bernstein.core.tokens.context_degradation_detector")
