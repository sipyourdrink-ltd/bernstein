"""Backward-compatibility shim — moved to bernstein.core.quality.ab_test."""

import importlib as _importlib

from bernstein.core.quality.ab_test import *  # noqa: F403

_real = _importlib.import_module("bernstein.core.quality.ab_test")


def __getattr__(name: str):
    return getattr(_real, name)
