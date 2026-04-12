"""Backward-compatibility shim — moved to bernstein.core.routing.auto_mode_classifier."""

import importlib as _importlib

from bernstein.core.routing.auto_mode_classifier import *  # noqa: F403

_real = _importlib.import_module("bernstein.core.routing.auto_mode_classifier")


def __getattr__(name: str):
    return getattr(_real, name)
