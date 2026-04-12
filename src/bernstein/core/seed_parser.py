"""Backward-compatibility shim — moved to bernstein.core.config.seed_parser."""
from bernstein.core.config.seed_parser import *  # noqa: F401,F403

import importlib as _importlib
_real = _importlib.import_module("bernstein.core.config.seed_parser")
def __getattr__(name: str):
    return getattr(_real, name)
