"""Backward-compatibility shim — moved to bernstein.core.persistence.workspace."""

import importlib as _importlib

from bernstein.core.persistence.workspace import *  # noqa: F403

_real = _importlib.import_module("bernstein.core.persistence.workspace")


def __getattr__(name: str):
    return getattr(_real, name)
