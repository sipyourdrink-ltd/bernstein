"""Backward-compatibility shim — moved to bernstein.core.routing.cascade."""

import importlib as _importlib

from bernstein.core.routing.cascade import *  # noqa: F403

_real = _importlib.import_module("bernstein.core.routing.cascade")


def __getattr__(name: str):
    return getattr(_real, name)
