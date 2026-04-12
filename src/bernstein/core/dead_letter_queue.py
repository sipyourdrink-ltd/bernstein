"""Backward-compatibility shim — moved to bernstein.core.tasks.dead_letter_queue."""

import importlib as _importlib

from bernstein.core.tasks.dead_letter_queue import *  # noqa: F403

_real = _importlib.import_module("bernstein.core.tasks.dead_letter_queue")


def __getattr__(name: str):
    return getattr(_real, name)
