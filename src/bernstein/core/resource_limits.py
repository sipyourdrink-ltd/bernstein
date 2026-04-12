"""Backward-compatibility shim — moved to bernstein.core.security.resource_limits."""

import importlib as _importlib

from bernstein.core.security.resource_limits import *  # noqa: F403

_real = _importlib.import_module("bernstein.core.security.resource_limits")


def __getattr__(name: str):
    return getattr(_real, name)
