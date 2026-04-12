"""Backward-compatibility shim — moved to bernstein.core.orchestration.concurrency_guard."""

import importlib as _importlib

from bernstein.core.orchestration.concurrency_guard import *  # noqa: F403

_real = _importlib.import_module("bernstein.core.orchestration.concurrency_guard")


def __getattr__(name: str):
    return getattr(_real, name)
