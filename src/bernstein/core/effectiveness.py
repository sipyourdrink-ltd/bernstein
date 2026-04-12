"""Backward-compatibility shim — moved to bernstein.core.quality.effectiveness."""

import importlib as _importlib

from bernstein.core.quality.effectiveness import *  # noqa: F403

_real = _importlib.import_module("bernstein.core.quality.effectiveness")


def __getattr__(name: str):
    return getattr(_real, name)
