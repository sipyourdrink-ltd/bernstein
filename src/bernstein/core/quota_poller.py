"""Backward-compatibility shim — moved to bernstein.core.protocols.quota_poller."""
from bernstein.core.protocols.quota_poller import *  # noqa: F401,F403

import importlib as _importlib
_real = _importlib.import_module("bernstein.core.protocols.quota_poller")
def __getattr__(name: str):
    return getattr(_real, name)
