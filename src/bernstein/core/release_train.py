"""Backward-compatibility shim — moved to bernstein.core.quality.release_train."""

import importlib as _importlib

from bernstein.core.quality.release_train import *  # noqa: F403

_real = _importlib.import_module("bernstein.core.quality.release_train")


def __getattr__(name: str):
    return getattr(_real, name)
