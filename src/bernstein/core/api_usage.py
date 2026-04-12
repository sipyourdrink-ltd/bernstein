"""Backward-compatibility shim — moved to bernstein.core.cost.api_usage."""

import importlib as _importlib

from bernstein.core.cost.api_usage import *  # noqa: F403

_real = _importlib.import_module("bernstein.core.cost.api_usage")


def __getattr__(name: str):
    return getattr(_real, name)
