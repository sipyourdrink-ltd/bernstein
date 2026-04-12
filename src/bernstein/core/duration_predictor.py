"""Backward-compatibility shim — moved to bernstein.core.planning.duration_predictor."""

from bernstein.core._shim import install_shim

install_shim(__name__, "bernstein.core.planning.duration_predictor")
