"""Backward-compatibility shim — moved to bernstein.core.orchestration.orchestrator_evolve."""
from bernstein.core.orchestration.orchestrator_evolve import *  # noqa: F401,F403

import importlib as _importlib
_real = _importlib.import_module("bernstein.core.orchestration.orchestrator_evolve")
def __getattr__(name: str):
    return getattr(_real, name)
