"""Backward-compatibility shim — moved to bernstein.core.communication.bulletin."""

import importlib as _importlib

from bernstein.core.communication.bulletin import *  # noqa: F403

_real = _importlib.import_module("bernstein.core.communication.bulletin")


def __getattr__(name: str):
    return getattr(_real, name)
