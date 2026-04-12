"""Backward-compatibility shim — moved to bernstein.core.planning.workflow_dsl."""
from bernstein.core.planning.workflow_dsl import *  # noqa: F401,F403

import importlib as _importlib
_real = _importlib.import_module("bernstein.core.planning.workflow_dsl")
def __getattr__(name: str):
    return getattr(_real, name)
