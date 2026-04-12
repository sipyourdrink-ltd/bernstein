"""Backward-compatibility shim — moved to bernstein.core.protocols.quota_probe."""
import importlib as _importlib

from bernstein.core.protocols.quota_probe import *  # noqa: F403

_real = _importlib.import_module("bernstein.core.protocols.quota_probe")
def __getattr__(name: str):
    return getattr(_real, name)
