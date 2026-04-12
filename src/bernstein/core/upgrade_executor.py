"""Backward-compatibility shim — moved to bernstein.core.config.upgrade_executor."""
import importlib as _importlib

from bernstein.core.config.upgrade_executor import *  # noqa: F403

_real = _importlib.import_module("bernstein.core.config.upgrade_executor")
def __getattr__(name: str):
    return getattr(_real, name)
