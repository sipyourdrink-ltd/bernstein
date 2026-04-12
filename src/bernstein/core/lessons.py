"""Backward-compatibility shim — moved to bernstein.core.knowledge.lessons."""

import importlib as _importlib

from bernstein.core.knowledge.lessons import *  # noqa: F403

_real = _importlib.import_module("bernstein.core.knowledge.lessons")


def __getattr__(name: str):
    return getattr(_real, name)
