"""Backward-compatibility shim — moved to bernstein.core.git.pr_size_governor."""

import importlib as _importlib

from bernstein.core.git.pr_size_governor import *  # noqa: F403

_real = _importlib.import_module("bernstein.core.git.pr_size_governor")


def __getattr__(name: str):
    return getattr(_real, name)
