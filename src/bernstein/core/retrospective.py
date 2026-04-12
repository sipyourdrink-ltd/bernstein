"""Backward-compatibility shim — moved to bernstein.core.quality.retrospective."""

import importlib as _importlib

from bernstein.core.quality.retrospective import *  # noqa: F403

_real = _importlib.import_module("bernstein.core.quality.retrospective")


def __getattr__(name: str):
    return getattr(_real, name)
