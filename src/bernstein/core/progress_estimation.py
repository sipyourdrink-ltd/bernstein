"""Backward-compatibility shim — moved to bernstein.core.planning.progress_estimation."""

from bernstein.core._shim import install_shim

install_shim(__name__, "bernstein.core.planning.progress_estimation")
