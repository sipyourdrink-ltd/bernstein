"""Backward-compatibility shim — moved to bernstein.core.git.incremental_merge."""

import importlib as _importlib

from bernstein.core.git.incremental_merge import *  # noqa: F403

_real = _importlib.import_module("bernstein.core.git.incremental_merge")


def __getattr__(name: str):
    return getattr(_real, name)
