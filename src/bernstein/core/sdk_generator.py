"""Backward-compatibility shim — moved to bernstein.core.plugins_core.sdk_generator."""

import importlib as _importlib

from bernstein.core.plugins_core.sdk_generator import *  # noqa: F403

_real = _importlib.import_module("bernstein.core.plugins_core.sdk_generator")


def __getattr__(name: str):
    return getattr(_real, name)
