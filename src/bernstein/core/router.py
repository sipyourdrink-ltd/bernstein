"""Backward-compatibility shim — moved to bernstein.core.routing.router."""

import importlib as _importlib

from bernstein.core.routing.router import *  # noqa: F403

_real = _importlib.import_module("bernstein.core.routing.router")


def __getattr__(name: str):
    return getattr(_real, name)
