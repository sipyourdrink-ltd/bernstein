"""Backward-compatibility shim — moved to bernstein.core.persistence.store."""

import importlib as _importlib

from bernstein.core.persistence.store import *  # noqa: F403

_real = _importlib.import_module("bernstein.core.persistence.store")


def __getattr__(name: str):
    return getattr(_real, name)
