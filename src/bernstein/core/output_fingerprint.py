"""Backward-compatibility shim — moved to bernstein.core.quality.output_fingerprint."""

import importlib as _importlib

from bernstein.core.quality.output_fingerprint import *  # noqa: F403

_real = _importlib.import_module("bernstein.core.quality.output_fingerprint")


def __getattr__(name: str):
    return getattr(_real, name)
