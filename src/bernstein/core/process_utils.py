"""Backward-compatibility shim — moved to bernstein.core.orchestration.process_utils."""
from bernstein.core.orchestration.process_utils import *  # noqa: F401,F403

import importlib as _importlib
_real = _importlib.import_module("bernstein.core.orchestration.process_utils")
def __getattr__(name: str):
    return getattr(_real, name)
