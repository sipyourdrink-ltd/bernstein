"""Backward-compatibility shim — moved to bernstein.core.knowledge.knowledge_base."""

import importlib as _importlib

from bernstein.core.knowledge.knowledge_base import *  # noqa: F403

_real = _importlib.import_module("bernstein.core.knowledge.knowledge_base")


def __getattr__(name: str):
    return getattr(_real, name)
