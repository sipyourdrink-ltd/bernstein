"""Backward-compatibility shim — moved to bernstein.core.quality.case_study."""
from bernstein.core.quality.case_study import *  # noqa: F401,F403

import importlib as _importlib
_real = _importlib.import_module("bernstein.core.quality.case_study")
def __getattr__(name: str):
    return getattr(_real, name)
