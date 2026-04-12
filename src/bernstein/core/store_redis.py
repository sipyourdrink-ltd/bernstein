"""Backward-compatibility shim — moved to bernstein.core.persistence.store_redis."""
from bernstein.core.persistence.store_redis import *  # noqa: F401,F403

import importlib as _importlib
_real = _importlib.import_module("bernstein.core.persistence.store_redis")
def __getattr__(name: str):
    return getattr(_real, name)
