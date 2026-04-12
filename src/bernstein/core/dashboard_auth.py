"""Backward-compatibility shim — moved to bernstein.core.server.dashboard_auth."""
from bernstein.core.server.dashboard_auth import *  # noqa: F401,F403

import importlib as _importlib
_real = _importlib.import_module("bernstein.core.server.dashboard_auth")
def __getattr__(name: str):
    return getattr(_real, name)
