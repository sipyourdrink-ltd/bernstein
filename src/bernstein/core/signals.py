"""Backward-compatibility shim — moved to bernstein.core.communication.signals."""

import importlib as _importlib

from bernstein.core.communication.signals import *  # noqa: F403

_real = _importlib.import_module("bernstein.core.communication.signals")


def __getattr__(name: str):
    return getattr(_real, name)
