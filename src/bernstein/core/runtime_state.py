"""Backward-compatibility shim — moved to bernstein.core.persistence.runtime_state."""

import importlib as _importlib

from bernstein.core.persistence.runtime_state import *  # noqa: F403

_real = _importlib.import_module("bernstein.core.persistence.runtime_state")


def __getattr__(name: str):
    return getattr(_real, name)
