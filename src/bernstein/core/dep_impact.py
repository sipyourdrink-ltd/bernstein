"""Backward-compatibility shim — moved to bernstein.core.quality.dep_impact."""

import importlib as _importlib

from bernstein.core.quality.dep_impact import *  # noqa: F403

_real = _importlib.import_module("bernstein.core.quality.dep_impact")


def __getattr__(name: str):
    return getattr(_real, name)
