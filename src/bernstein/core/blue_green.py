"""Backward-compatibility shim — moved to bernstein.core.orchestration.blue_green."""

import importlib as _importlib

from bernstein.core.orchestration.blue_green import *  # noqa: F403

_real = _importlib.import_module("bernstein.core.orchestration.blue_green")


def __getattr__(name: str):
    return getattr(_real, name)
