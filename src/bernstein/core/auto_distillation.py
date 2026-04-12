"""Backward-compatibility shim — moved to bernstein.core.tokens.auto_distillation."""
from bernstein.core.tokens.auto_distillation import *  # noqa: F401,F403

import importlib as _importlib
_real = _importlib.import_module("bernstein.core.tokens.auto_distillation")
def __getattr__(name: str):
    return getattr(_real, name)
