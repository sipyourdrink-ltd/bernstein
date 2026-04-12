"""Backward-compatibility shim — moved to bernstein.core.config.white_label."""

import importlib as _importlib

from bernstein.core.config.white_label import *  # noqa: F403

_real = _importlib.import_module("bernstein.core.config.white_label")


def __getattr__(name: str):
    return getattr(_real, name)
