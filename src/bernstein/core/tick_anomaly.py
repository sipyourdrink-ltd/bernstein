"""Backward-compatibility shim — moved to bernstein.core.observability.tick_anomaly."""
from bernstein.core.observability.tick_anomaly import *  # noqa: F401,F403

import importlib as _importlib
_real = _importlib.import_module("bernstein.core.observability.tick_anomaly")
def __getattr__(name: str):
    return getattr(_real, name)
