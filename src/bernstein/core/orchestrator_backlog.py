"""Backward-compatibility shim — moved to bernstein.core.orchestration.orchestrator_backlog."""
from bernstein.core.orchestration.orchestrator_backlog import *  # noqa: F401,F403

import importlib as _importlib
_real = _importlib.import_module("bernstein.core.orchestration.orchestrator_backlog")
def __getattr__(name: str):
    return getattr(_real, name)
