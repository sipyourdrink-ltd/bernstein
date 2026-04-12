"""Backward-compatibility shim — moved to bernstein.core.orchestration.multi_cell."""

import importlib as _importlib

from bernstein.core.orchestration.multi_cell import *  # noqa: F403

_real = _importlib.import_module("bernstein.core.orchestration.multi_cell")


def __getattr__(name: str):
    return getattr(_real, name)
