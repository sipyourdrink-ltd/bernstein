"""Backward-compatibility shim — moved to bernstein.core.quality.decomposition_validator."""

import importlib as _importlib

from bernstein.core.quality.decomposition_validator import *  # noqa: F403

_real = _importlib.import_module("bernstein.core.quality.decomposition_validator")


def __getattr__(name: str):
    return getattr(_real, name)
