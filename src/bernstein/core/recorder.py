"""Backward-compatibility shim — moved to bernstein.core.persistence.recorder."""

import importlib as _importlib

from bernstein.core.persistence.recorder import *  # noqa: F403

_real = _importlib.import_module("bernstein.core.persistence.recorder")


def __getattr__(name: str):
    return getattr(_real, name)
