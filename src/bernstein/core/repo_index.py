"""Backward-compatibility shim — moved to bernstein.core.knowledge.repo_index."""

import importlib as _importlib

from bernstein.core.knowledge.repo_index import *  # noqa: F403

_real = _importlib.import_module("bernstein.core.knowledge.repo_index")


def __getattr__(name: str):
    return getattr(_real, name)
