"""Backward-compatibility shim — moved to bernstein.core.config.recipe."""

import importlib as _importlib

from bernstein.core.config.recipe import *  # noqa: F403

_real = _importlib.import_module("bernstein.core.config.recipe")


def __getattr__(name: str):
    return getattr(_real, name)
