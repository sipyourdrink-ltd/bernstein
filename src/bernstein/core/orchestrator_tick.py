"""Backward-compatibility shim — moved to bernstein.core.orchestration.orchestrator_tick."""
from bernstein.core.orchestration.orchestrator_tick import *  # noqa: F401,F403

import importlib as _importlib
_real = _importlib.import_module("bernstein.core.orchestration.orchestrator_tick")
def __getattr__(name: str):
    return getattr(_real, name)
