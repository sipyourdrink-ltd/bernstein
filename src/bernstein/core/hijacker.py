"""Backward-compatibility shim — moved to bernstein.core.routing.hijacker."""

import importlib as _importlib

from bernstein.core.routing.hijacker import *  # noqa: F403

_real = _importlib.import_module("bernstein.core.routing.hijacker")


def __getattr__(name: str):
    return getattr(_real, name)
