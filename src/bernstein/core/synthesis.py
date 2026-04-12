"""Backward-compatibility shim — moved to bernstein.core.knowledge.synthesis."""

import importlib as _importlib

from bernstein.core.knowledge.synthesis import *  # noqa: F403

_real = _importlib.import_module("bernstein.core.knowledge.synthesis")


def __getattr__(name: str):
    return getattr(_real, name)
