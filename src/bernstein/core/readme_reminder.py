"""Backward-compatibility shim — moved to bernstein.core.quality.readme_reminder."""
from bernstein.core.quality.readme_reminder import *  # noqa: F401,F403

import importlib as _importlib
_real = _importlib.import_module("bernstein.core.quality.readme_reminder")
def __getattr__(name: str):
    return getattr(_real, name)
