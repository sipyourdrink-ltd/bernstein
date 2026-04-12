"""Backward-compatibility shim — moved to bernstein.core.security.auth."""

import importlib as _importlib

from bernstein.core.security.auth import *  # noqa: F403

_real = _importlib.import_module("bernstein.core.security.auth")


def __getattr__(name: str):
    return getattr(_real, name)
