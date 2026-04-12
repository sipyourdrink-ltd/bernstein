"""Backward-compatibility shim — moved to bernstein.core.routing.batch_router."""

import importlib as _importlib

from bernstein.core.routing.batch_router import *  # noqa: F403

_real = _importlib.import_module("bernstein.core.routing.batch_router")


def __getattr__(name: str):
    return getattr(_real, name)
