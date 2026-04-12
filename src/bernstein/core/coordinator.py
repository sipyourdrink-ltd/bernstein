"""Backward-compatibility shim — moved to bernstein.core.orchestration.coordinator."""

import importlib as _importlib

from bernstein.core.orchestration.coordinator import *  # noqa: F403

_real = _importlib.import_module("bernstein.core.orchestration.coordinator")


def __getattr__(name: str):
    return getattr(_real, name)
