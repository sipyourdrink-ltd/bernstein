"""Backward-compatibility shim — moved to bernstein.core.routing.cascade_router."""

import importlib as _importlib

from bernstein.core.routing.cascade_router import *  # noqa: F403

_real = _importlib.import_module("bernstein.core.routing.cascade_router")


def __getattr__(name: str):
    return getattr(_real, name)
