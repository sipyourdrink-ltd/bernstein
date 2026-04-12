"""Backward-compatibility shim — moved to bernstein.core.config.hook_protocol."""

import importlib as _importlib

from bernstein.core.config.hook_protocol import *  # noqa: F403

_real = _importlib.import_module("bernstein.core.config.hook_protocol")


def __getattr__(name: str):
    return getattr(_real, name)
