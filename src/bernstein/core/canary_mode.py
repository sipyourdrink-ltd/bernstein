"""Backward-compatibility shim — moved to bernstein.core.orchestration.canary_mode."""
import importlib as _importlib

from bernstein.core.orchestration.canary_mode import *  # noqa: F403

_real = _importlib.import_module("bernstein.core.orchestration.canary_mode")
def __getattr__(name: str):
    return getattr(_real, name)
