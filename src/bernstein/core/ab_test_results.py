"""Backward-compatibility shim — moved to bernstein.core.quality.ab_test_results."""
from bernstein.core.quality.ab_test_results import *  # noqa: F401,F403

import importlib as _importlib
_real = _importlib.import_module("bernstein.core.quality.ab_test_results")
def __getattr__(name: str):
    return getattr(_real, name)
