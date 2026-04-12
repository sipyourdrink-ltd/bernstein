"""Backward-compatibility shim — moved to bernstein.core.persistence.wal."""

import importlib as _importlib

from bernstein.core.persistence.wal import *  # noqa: F403

_real = _importlib.import_module("bernstein.core.persistence.wal")


def __getattr__(name: str):
    return getattr(_real, name)
