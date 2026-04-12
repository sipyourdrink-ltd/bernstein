"""Backward-compatibility shim — moved to bernstein.core.plugins_core.plugin_manifest."""
from bernstein.core.plugins_core.plugin_manifest import *  # noqa: F401,F403

import importlib as _importlib
_real = _importlib.import_module("bernstein.core.plugins_core.plugin_manifest")
def __getattr__(name: str):
    return getattr(_real, name)
