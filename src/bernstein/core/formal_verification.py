"""Backward-compatibility shim — moved to bernstein.core.quality.formal_verification."""

import importlib as _importlib

from bernstein.core.quality.formal_verification import *  # noqa: F403

_real = _importlib.import_module("bernstein.core.quality.formal_verification")


def __getattr__(name: str):
    return getattr(_real, name)
