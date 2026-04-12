"""Backward-compatibility shim — moved to bernstein.core.quality.consensus_verifier."""
from bernstein.core.quality.consensus_verifier import *  # noqa: F401,F403

import importlib as _importlib
_real = _importlib.import_module("bernstein.core.quality.consensus_verifier")
def __getattr__(name: str):
    return getattr(_real, name)
