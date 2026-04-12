"""Backward-compatibility shim — moved to bernstein.core.quality.gate_commands."""

import importlib as _importlib

from bernstein.core.quality.gate_commands import *  # noqa: F403

_real = _importlib.import_module("bernstein.core.quality.gate_commands")


def __getattr__(name: str):
    return getattr(_real, name)
