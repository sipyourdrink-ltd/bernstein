"""Backward-compatibility shim — moved to bernstein.core.config.upgrade_executor."""
from bernstein.core.config.upgrade_executor import *  # noqa: F401,F403

import importlib as _importlib
_real = _importlib.import_module("bernstein.core.config.upgrade_executor")
def __getattr__(name: str):
    return getattr(_real, name)
