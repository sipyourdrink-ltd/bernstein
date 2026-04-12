"""Backward-compatibility shim — moved to bernstein.core.knowledge.memory_lock_protocol."""

import importlib as _importlib

from bernstein.core.knowledge.memory_lock_protocol import *  # noqa: F403

_real = _importlib.import_module("bernstein.core.knowledge.memory_lock_protocol")


def __getattr__(name: str):
    return getattr(_real, name)
