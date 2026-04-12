"""Backward-compatibility shim — moved to bernstein.core.tasks.task_retry."""

import importlib as _importlib

from bernstein.core.tasks.task_retry import *  # noqa: F403

_real = _importlib.import_module("bernstein.core.tasks.task_retry")


def __getattr__(name: str):
    return getattr(_real, name)
