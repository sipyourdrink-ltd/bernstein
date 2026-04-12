"""Backward-compatibility shim — moved to bernstein.core.quality.verification_nudge."""
from bernstein.core.quality.verification_nudge import *  # noqa: F401,F403

import importlib as _importlib
_real = _importlib.import_module("bernstein.core.quality.verification_nudge")
def __getattr__(name: str):
    return getattr(_real, name)
