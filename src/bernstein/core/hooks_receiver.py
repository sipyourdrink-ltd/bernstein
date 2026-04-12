"""Backward-compatibility shim — moved to bernstein.core.server.hooks_receiver."""

import importlib as _importlib

from bernstein.core.server.hooks_receiver import *  # noqa: F403

_real = _importlib.import_module("bernstein.core.server.hooks_receiver")


def __getattr__(name: str):
    return getattr(_real, name)
