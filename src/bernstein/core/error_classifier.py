"""Backward-compatibility shim — moved to bernstein.core.observability.error_classifier."""

import importlib as _importlib

from bernstein.core.observability.error_classifier import *  # noqa: F403

_real = _importlib.import_module("bernstein.core.observability.error_classifier")


def __getattr__(name: str):
    return getattr(_real, name)
