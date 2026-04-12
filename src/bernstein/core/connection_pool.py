"""Backward-compatibility shim — moved to bernstein.core.server.connection_pool."""

import importlib as _importlib

from bernstein.core.server.connection_pool import *  # noqa: F403

_real = _importlib.import_module("bernstein.core.server.connection_pool")


def __getattr__(name: str):
    return getattr(_real, name)
