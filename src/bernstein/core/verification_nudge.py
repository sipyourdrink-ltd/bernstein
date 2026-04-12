"""Backward-compatibility shim — moved to bernstein.core.quality.verification_nudge."""

import importlib as _importlib

from bernstein.core.quality.verification_nudge import *  # noqa: F403

_real = _importlib.import_module("bernstein.core.quality.verification_nudge")


def __getattr__(name: str):
    return getattr(_real, name)
