"""Backward-compatibility shim — moved to bernstein.core.orchestration.manager_models."""
from bernstein.core.orchestration.manager_models import *  # noqa: F401,F403

import importlib as _importlib
_real = _importlib.import_module("bernstein.core.orchestration.manager_models")
def __getattr__(name: str):
    return getattr(_real, name)
