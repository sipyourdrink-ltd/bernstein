"""Backward-compatibility shim — moved to bernstein.core.security.quarantine."""

import importlib as _importlib

from bernstein.core.security.quarantine import *  # noqa: F403

_real = _importlib.import_module("bernstein.core.security.quarantine")


def __getattr__(name: str):
    return getattr(_real, name)
