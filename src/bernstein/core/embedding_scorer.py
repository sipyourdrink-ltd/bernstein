"""Backward-compatibility shim — moved to bernstein.core.knowledge.embedding_scorer."""

import importlib as _importlib

from bernstein.core.knowledge.embedding_scorer import *  # noqa: F403

_real = _importlib.import_module("bernstein.core.knowledge.embedding_scorer")


def __getattr__(name: str):
    return getattr(_real, name)
