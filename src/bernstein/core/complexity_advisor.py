"""Backward-compatibility shim — moved to bernstein.core.quality.complexity_advisor."""

import importlib as _importlib

from bernstein.core.quality.complexity_advisor import *  # noqa: F403

_real = _importlib.import_module("bernstein.core.quality.complexity_advisor")


def __getattr__(name: str):
    return getattr(_real, name)
