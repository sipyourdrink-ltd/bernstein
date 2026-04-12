"""Backward-compatibility shim — moved to bernstein.core.planning.progress_estimation."""

import importlib as _importlib

from bernstein.core.planning.progress_estimation import *  # noqa: F403

_real = _importlib.import_module("bernstein.core.planning.progress_estimation")


def __getattr__(name: str):
    return getattr(_real, name)
