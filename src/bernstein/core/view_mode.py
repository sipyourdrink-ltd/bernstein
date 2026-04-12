"""Backward-compatibility shim — moved to bernstein.core.config.view_mode."""

import importlib as _importlib

from bernstein.core.config.view_mode import *  # noqa: F403

_real = _importlib.import_module("bernstein.core.config.view_mode")


def __getattr__(name: str):
    return getattr(_real, name)
