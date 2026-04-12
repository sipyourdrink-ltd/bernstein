"""Backward-compat shim -- real module moved to bernstein.core.tasks.batch_router."""

from bernstein.core._shim import install_shim

install_shim(__name__, "bernstein.core.tasks.batch_router")
