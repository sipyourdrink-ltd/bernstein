"""Backward-compatibility shim — moved to bernstein.core.observability.health_score."""
from bernstein.core.observability.health_score import *  # noqa: F401,F403

import importlib as _importlib
_real = _importlib.import_module("bernstein.core.observability.health_score")
def __getattr__(name: str):
    return getattr(_real, name)
