"""Backward-compatibility shim — moved to bernstein.core.security.sbom."""
from bernstein.core.security.sbom import *  # noqa: F401,F403

import importlib as _importlib
_real = _importlib.import_module("bernstein.core.security.sbom")
def __getattr__(name: str):
    return getattr(_real, name)
