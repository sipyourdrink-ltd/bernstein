"""Backward-compatibility shim — moved to bernstein.core.git.changelog."""

import importlib as _importlib

from bernstein.core.git.changelog import *  # noqa: F403

_real = _importlib.import_module("bernstein.core.git.changelog")


def __getattr__(name: str):
    return getattr(_real, name)
