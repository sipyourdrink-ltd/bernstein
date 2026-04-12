"""Backward-compatibility shim — moved to bernstein.core.quality.ci_log_parser."""
from bernstein.core.quality.ci_log_parser import *  # noqa: F401,F403

import importlib as _importlib
_real = _importlib.import_module("bernstein.core.quality.ci_log_parser")
def __getattr__(name: str):
    return getattr(_real, name)
