"""Backward-compat shim -- real module moved to bernstein.core.tasks.task_store."""

from bernstein.core._shim import install_shim

install_shim(__name__, "bernstein.core.tasks.task_store")
