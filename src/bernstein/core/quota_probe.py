"""Backward-compatibility shim — moved to bernstein.core.protocols.quota_probe."""
from bernstein.core.protocols.quota_probe import *  # noqa: F401,F403

import importlib as _importlib
_real = _importlib.import_module("bernstein.core.protocols.quota_probe")
def __getattr__(name: str):
    return getattr(_real, name)
