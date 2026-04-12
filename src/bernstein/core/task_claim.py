"""Backward-compatibility shim — moved to bernstein.core.tasks.task_claim."""

from bernstein.core._shim import install_shim

install_shim(__name__, "bernstein.core.tasks.task_claim")
