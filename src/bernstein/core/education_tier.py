"""Backward-compatibility shim — moved to bernstein.core.cost.education_tier."""

import importlib as _importlib

from bernstein.core.cost.education_tier import *  # noqa: F403

_real = _importlib.import_module("bernstein.core.cost.education_tier")


def __getattr__(name: str):
    return getattr(_real, name)
