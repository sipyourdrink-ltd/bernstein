"""Backward-compatibility shim — moved to bernstein.core.security.audit."""

import importlib as _importlib

from bernstein.core.security.audit import *  # noqa: F403

_real = _importlib.import_module("bernstein.core.security.audit")


def __getattr__(name: str):
    return getattr(_real, name)
