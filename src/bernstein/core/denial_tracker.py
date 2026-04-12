"""Backward-compatibility shim — moved to bernstein.core.security.denial_tracker."""

import importlib as _importlib

from bernstein.core.security.denial_tracker import *  # noqa: F403

_real = _importlib.import_module("bernstein.core.security.denial_tracker")


def __getattr__(name: str):
    return getattr(_real, name)
