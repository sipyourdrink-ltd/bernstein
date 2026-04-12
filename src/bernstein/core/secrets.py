"""Backward-compatibility shim — moved to bernstein.core.security.secrets."""

import importlib as _importlib

from bernstein.core.security.secrets import *  # noqa: F403

_real = _importlib.import_module("bernstein.core.security.secrets")


def __getattr__(name: str):
    return getattr(_real, name)
