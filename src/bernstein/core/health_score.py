"""Backward-compatibility shim — moved to bernstein.core.observability.health_score."""

import importlib as _importlib

from bernstein.core.observability.health_score import *  # noqa: F403

_real = _importlib.import_module("bernstein.core.observability.health_score")


def __getattr__(name: str):
    return getattr(_real, name)
