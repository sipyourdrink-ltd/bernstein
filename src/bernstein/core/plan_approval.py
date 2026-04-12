"""Backward-compatibility shim — moved to bernstein.core.security.plan_approval."""
from bernstein.core.security.plan_approval import *  # noqa: F401,F403

import importlib as _importlib
_real = _importlib.import_module("bernstein.core.security.plan_approval")
def __getattr__(name: str):
    return getattr(_real, name)
