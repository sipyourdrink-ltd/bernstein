"""Backward-compatibility shim — moved to bernstein.core.config.platform_compat."""
import importlib as _importlib

from bernstein.core.config.platform_compat import *  # noqa: F403

_real = _importlib.import_module("bernstein.core.config.platform_compat")
def __getattr__(name: str):
    return getattr(_real, name)
