"""Backward-compatibility shim — moved to bernstein.core.orchestration.orchestrator_run."""
from bernstein.core.orchestration.orchestrator_run import *  # noqa: F401,F403

import importlib as _importlib
_real = _importlib.import_module("bernstein.core.orchestration.orchestrator_run")
def __getattr__(name: str):
    return getattr(_real, name)
