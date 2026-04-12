"""Backward-compatibility shim — moved to bernstein.core.orchestration.capacity_wake."""

import importlib as _importlib

from bernstein.core.orchestration.capacity_wake import *  # noqa: F403

_real = _importlib.import_module("bernstein.core.orchestration.capacity_wake")


def __getattr__(name: str):
    return getattr(_real, name)
