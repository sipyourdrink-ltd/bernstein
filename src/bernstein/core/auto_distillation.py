"""Backward-compatibility shim — moved to bernstein.core.tokens.auto_distillation."""

import importlib as _importlib

from bernstein.core.tokens.auto_distillation import *  # noqa: F403

_real = _importlib.import_module("bernstein.core.tokens.auto_distillation")


def __getattr__(name: str):
    return getattr(_real, name)
