"""Backward-compatibility shim — moved to bernstein.core.config.home."""

import importlib as _importlib

from bernstein.core.config.home import *  # noqa: F403

_real = _importlib.import_module("bernstein.core.config.home")


def __getattr__(name: str):
    return getattr(_real, name)
