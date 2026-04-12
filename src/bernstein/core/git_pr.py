"""Backward-compatibility shim — moved to bernstein.core.git.git_pr."""

import importlib as _importlib

from bernstein.core.git.git_pr import *  # noqa: F403

_real = _importlib.import_module("bernstein.core.git.git_pr")


def __getattr__(name: str):
    return getattr(_real, name)
