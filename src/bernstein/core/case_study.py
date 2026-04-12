"""Backward-compatibility shim — moved to bernstein.core.quality.case_study."""

import importlib as _importlib

from bernstein.core.quality.case_study import *  # noqa: F403

_real = _importlib.import_module("bernstein.core.quality.case_study")


def __getattr__(name: str):
    return getattr(_real, name)
