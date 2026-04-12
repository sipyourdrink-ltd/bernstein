"""Backward-compatibility shim — moved to bernstein.core.orchestration.operator."""

import importlib as _importlib

from bernstein.core.orchestration.operator import *  # noqa: F403

_real = _importlib.import_module("bernstein.core.orchestration.operator")


def __getattr__(name: str):
    return getattr(_real, name)
