"""Backward-compatibility shim — moved to bernstein.core.security.eu_ai_act."""
from bernstein.core.security.eu_ai_act import *  # noqa: F401,F403

import importlib as _importlib
_real = _importlib.import_module("bernstein.core.security.eu_ai_act")
def __getattr__(name: str):
    return getattr(_real, name)
