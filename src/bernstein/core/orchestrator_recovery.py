"""Backward-compatibility shim — moved to bernstein.core.orchestration.orchestrator_recovery."""
from bernstein.core.orchestration.orchestrator_recovery import *  # noqa: F401,F403

import importlib as _importlib
_real = _importlib.import_module("bernstein.core.orchestration.orchestrator_recovery")
def __getattr__(name: str):
    return getattr(_real, name)
