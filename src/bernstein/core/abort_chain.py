"""Backward-compatibility shim — moved to bernstein.core.tasks.abort_chain."""

import importlib as _importlib

from bernstein.core.tasks.abort_chain import *  # noqa: F403

_real = _importlib.import_module("bernstein.core.tasks.abort_chain")


def __getattr__(name: str):
    return getattr(_real, name)
