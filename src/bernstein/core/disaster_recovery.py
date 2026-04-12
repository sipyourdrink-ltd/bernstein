"""Backward-compatibility shim — moved to bernstein.core.persistence.disaster_recovery."""

import importlib as _importlib

from bernstein.core.persistence.disaster_recovery import *  # noqa: F403

_real = _importlib.import_module("bernstein.core.persistence.disaster_recovery")


def __getattr__(name: str):
    return getattr(_real, name)
