"""Backward-compatibility shim — moved to bernstein.core.config.platform_compat."""
from bernstein.core.config.platform_compat import *  # noqa: F401,F403

import importlib as _importlib
_real = _importlib.import_module("bernstein.core.config.platform_compat")
def __getattr__(name: str):
    return getattr(_real, name)
