"""Backward-compatibility shim — moved to bernstein.core.server.api_compat_checker."""

import importlib as _importlib

from bernstein.core.server.api_compat_checker import *  # noqa: F403

_real = _importlib.import_module("bernstein.core.server.api_compat_checker")


def __getattr__(name: str):
    return getattr(_real, name)
