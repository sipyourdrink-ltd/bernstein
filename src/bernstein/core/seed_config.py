"""Backward-compatibility shim — moved to bernstein.core.config.seed_config."""
from bernstein.core.config.seed_config import *  # noqa: F401,F403

import importlib as _importlib
_real = _importlib.import_module("bernstein.core.config.seed_config")
def __getattr__(name: str):
    return getattr(_real, name)
