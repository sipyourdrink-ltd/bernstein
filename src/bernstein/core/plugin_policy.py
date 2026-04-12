"""Backward-compatibility shim — moved to bernstein.core.security.plugin_policy."""
from bernstein.core.security.plugin_policy import *  # noqa: F401,F403

import importlib as _importlib
_real = _importlib.import_module("bernstein.core.security.plugin_policy")
def __getattr__(name: str):
    return getattr(_real, name)
