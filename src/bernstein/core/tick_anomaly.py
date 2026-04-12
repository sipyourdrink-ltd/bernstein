"""Backward-compatibility shim — moved to bernstein.core.observability.tick_anomaly."""

import importlib as _importlib

from bernstein.core.observability.tick_anomaly import *  # noqa: F403

_real = _importlib.import_module("bernstein.core.observability.tick_anomaly")


def __getattr__(name: str):
    return getattr(_real, name)
