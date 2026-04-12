"""Backward-compatibility shim — moved to bernstein.core.planning.duration_predictor."""

import importlib as _importlib

from bernstein.core.planning.duration_predictor import *  # noqa: F403

_real = _importlib.import_module("bernstein.core.planning.duration_predictor")


def __getattr__(name: str):
    return getattr(_real, name)
