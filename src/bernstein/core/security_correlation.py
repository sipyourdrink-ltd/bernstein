"""Backward-compatibility shim — moved to bernstein.core.security.security_correlation."""

import importlib as _importlib

from bernstein.core.security.security_correlation import *  # noqa: F403

_real = _importlib.import_module("bernstein.core.security.security_correlation")


def __getattr__(name: str):
    return getattr(_real, name)
