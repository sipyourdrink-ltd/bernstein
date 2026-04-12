"""Backward-compatibility shim — moved to bernstein.core.persistence.session_continuity."""

import importlib as _importlib

from bernstein.core.persistence.session_continuity import *  # noqa: F403

_real = _importlib.import_module("bernstein.core.persistence.session_continuity")


def __getattr__(name: str):
    return getattr(_real, name)
