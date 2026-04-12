"""Backward-compatibility shim — moved to bernstein.core.security.audit_export."""
from bernstein.core.security.audit_export import *  # noqa: F401,F403

import importlib as _importlib
_real = _importlib.import_module("bernstein.core.security.audit_export")
def __getattr__(name: str):
    return getattr(_real, name)
