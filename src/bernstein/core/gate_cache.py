"""Backward-compatibility shim — moved to bernstein.core.quality.gate_cache."""

import importlib as _importlib

from bernstein.core.quality.gate_cache import *  # noqa: F403

_real = _importlib.import_module("bernstein.core.quality.gate_cache")


def __getattr__(name: str):
    return getattr(_real, name)
