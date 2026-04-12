"""Backward-compatibility shim — moved to bernstein.core.git.git_hooks."""

import importlib as _importlib

from bernstein.core.git.git_hooks import *  # noqa: F403

_real = _importlib.import_module("bernstein.core.git.git_hooks")


def __getattr__(name: str):
    return getattr(_real, name)
