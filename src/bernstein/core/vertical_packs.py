"""Backward-compatibility shim — moved to bernstein.core.plugins_core.vertical_packs."""
from bernstein.core.plugins_core.vertical_packs import *  # noqa: F401,F403

import importlib as _importlib
_real = _importlib.import_module("bernstein.core.plugins_core.vertical_packs")
def __getattr__(name: str):
    return getattr(_real, name)
