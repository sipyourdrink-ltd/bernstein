"""Backward-compatibility shim — moved to bernstein.core.tasks.task_claim."""

import importlib as _importlib

from bernstein.core.tasks.task_claim import *  # noqa: F403

_real = _importlib.import_module("bernstein.core.tasks.task_claim")


def __getattr__(name: str):
    return getattr(_real, name)
