"""Backward-compatibility shim — moved to bernstein.core.git.git_context."""

import importlib as _importlib

from bernstein.core.git.git_context import *  # noqa: F403

_real = _importlib.import_module("bernstein.core.git.git_context")


def __getattr__(name: str):
    return getattr(_real, name)
