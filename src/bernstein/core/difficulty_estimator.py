"""Backward-compat shim -- real module moved to bernstein.core.tasks.difficulty_estimator."""

from bernstein.core._shim import install_shim

install_shim(__name__, "bernstein.core.tasks.difficulty_estimator")
