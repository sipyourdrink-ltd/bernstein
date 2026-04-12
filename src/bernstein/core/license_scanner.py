"""Backward-compatibility shim — moved to bernstein.core.security.license_scanner."""
import importlib as _importlib

from bernstein.core.security.license_scanner import *  # noqa: F403

_real = _importlib.import_module("bernstein.core.security.license_scanner")
def __getattr__(name: str):
    return getattr(_real, name)
