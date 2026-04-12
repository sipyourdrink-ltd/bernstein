"""Backward-compatibility shim — moved to bernstein.core.security.hipaa."""

import importlib as _importlib

from bernstein.core.security.hipaa import *  # noqa: F403

_real = _importlib.import_module("bernstein.core.security.hipaa")


def __getattr__(name: str):
    return getattr(_real, name)
