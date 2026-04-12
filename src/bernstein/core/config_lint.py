"""Backward-compatibility shim — moved to bernstein.core.config.config_lint."""
from bernstein.core.config.config_lint import *  # noqa: F401,F403

import importlib as _importlib
_real = _importlib.import_module("bernstein.core.config.config_lint")
def __getattr__(name: str):
    return getattr(_real, name)
