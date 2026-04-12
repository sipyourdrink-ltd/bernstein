"""Backward-compatibility shim — moved to bernstein.core.observability.startup_selftest."""

import importlib as _importlib

from bernstein.core.observability.startup_selftest import *  # noqa: F403

_real = _importlib.import_module("bernstein.core.observability.startup_selftest")


def __getattr__(name: str):
    return getattr(_real, name)
