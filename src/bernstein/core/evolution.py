"""Backward-compatibility shim — moved to bernstein.core.orchestration.evolution."""

import importlib as _importlib

from bernstein.core.orchestration.evolution import *  # noqa: F403

_real = _importlib.import_module("bernstein.core.orchestration.evolution")


def __getattr__(name: str):
    return getattr(_real, name)
