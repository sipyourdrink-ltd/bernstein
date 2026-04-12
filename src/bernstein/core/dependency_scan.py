"""Backward-compatibility shim — moved to bernstein.core.quality.dependency_scan."""

import importlib as _importlib

from bernstein.core.quality.dependency_scan import *  # noqa: F403

_real = _importlib.import_module("bernstein.core.quality.dependency_scan")


def __getattr__(name: str):
    return getattr(_real, name)
