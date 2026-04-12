"""Backward-compatibility shim — moved to bernstein.core.orchestration.worker."""

import importlib as _importlib

from bernstein.core.orchestration.worker import *  # noqa: F403

_real = _importlib.import_module("bernstein.core.orchestration.worker")


def __getattr__(name: str):
    return getattr(_real, name)
