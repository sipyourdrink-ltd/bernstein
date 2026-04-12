"""Backward-compatibility shim — moved to bernstein.core.git.merge_queue."""

import importlib as _importlib

from bernstein.core.git.merge_queue import *  # noqa: F403

_real = _importlib.import_module("bernstein.core.git.merge_queue")


def __getattr__(name: str):
    return getattr(_real, name)
