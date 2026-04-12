"""Backward-compatibility shim — moved to bernstein.core.quality.ci_monitor."""

import importlib as _importlib

from bernstein.core.quality.ci_monitor import *  # noqa: F403

_real = _importlib.import_module("bernstein.core.quality.ci_monitor")


def __getattr__(name: str):
    return getattr(_real, name)
