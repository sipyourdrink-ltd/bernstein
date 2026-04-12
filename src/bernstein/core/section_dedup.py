"""Backward-compatibility shim — moved to bernstein.core.quality.section_dedup."""

import importlib as _importlib

from bernstein.core.quality.section_dedup import *  # noqa: F403

_real = _importlib.import_module("bernstein.core.quality.section_dedup")


def __getattr__(name: str):
    return getattr(_real, name)
