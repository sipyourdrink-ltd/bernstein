"""Backward-compat shim -- real module moved to bernstein.core.tasks.batch_transitions."""

from bernstein.core._shim import install_shim

install_shim(__name__, "bernstein.core.tasks.batch_transitions")
