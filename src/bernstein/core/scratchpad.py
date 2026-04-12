"""Backward-compatibility shim — moved to bernstein.core.communication.scratchpad."""

import importlib as _importlib

from bernstein.core.communication.scratchpad import *  # noqa: F403

_real = _importlib.import_module("bernstein.core.communication.scratchpad")


def __getattr__(name: str):
    return getattr(_real, name)
