"""Backward-compatibility shim — moved to bernstein.core.config.i18n."""
from bernstein.core.config.i18n import *  # noqa: F401,F403

import importlib as _importlib
_real = _importlib.import_module("bernstein.core.config.i18n")
def __getattr__(name: str):
    return getattr(_real, name)
