"""Backward-compatibility shim — moved to bernstein.core.security.plugin_policy."""

import importlib as _importlib

from bernstein.core.security.plugin_policy import *  # noqa: F403

_real = _importlib.import_module("bernstein.core.security.plugin_policy")


def __getattr__(name: str):
    return getattr(_real, name)
