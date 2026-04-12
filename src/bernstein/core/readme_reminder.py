"""Backward-compatibility shim — moved to bernstein.core.quality.readme_reminder."""
import importlib as _importlib

from bernstein.core.quality.readme_reminder import *  # noqa: F403

_real = _importlib.import_module("bernstein.core.quality.readme_reminder")
def __getattr__(name: str):
    return getattr(_real, name)
