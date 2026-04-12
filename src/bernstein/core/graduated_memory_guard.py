"""Backward-compatibility shim — moved to bernstein.core.knowledge.graduated_memory_guard."""

import importlib as _importlib

from bernstein.core.knowledge.graduated_memory_guard import *  # noqa: F403

_real = _importlib.import_module("bernstein.core.knowledge.graduated_memory_guard")


def __getattr__(name: str):
    return getattr(_real, name)
