"""Backward-compatibility shim — moved to bernstein.core.security.sbom."""
import importlib as _importlib

from bernstein.core.security.sbom import *  # noqa: F403

_real = _importlib.import_module("bernstein.core.security.sbom")
def __getattr__(name: str):
    return getattr(_real, name)
