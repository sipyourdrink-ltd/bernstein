"""Backward-compatibility shim — moved to bernstein.core.security.soc2_report."""
from bernstein.core.security.soc2_report import *  # noqa: F401,F403

import importlib as _importlib
_real = _importlib.import_module("bernstein.core.security.soc2_report")
def __getattr__(name: str):
    return getattr(_real, name)
