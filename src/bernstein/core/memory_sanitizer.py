"""Backward-compatibility shim — moved to bernstein.core.knowledge.memory_sanitizer."""

import importlib as _importlib

from bernstein.core.knowledge.memory_sanitizer import *  # noqa: F403

_real = _importlib.import_module("bernstein.core.knowledge.memory_sanitizer")


def __getattr__(name: str):
    return getattr(_real, name)
