"""Backward-compatibility shim — moved to bernstein.core.tasks.dead_letter_queue."""

from bernstein.core._shim import install_shim

install_shim(__name__, "bernstein.core.tasks.dead_letter_queue")
