"""Backward-compatibility shim — moved to bernstein.core.orchestration.workload_prediction."""
from bernstein.core.orchestration.workload_prediction import *  # noqa: F401,F403

import importlib as _importlib
_real = _importlib.import_module("bernstein.core.orchestration.workload_prediction")
def __getattr__(name: str):
    return getattr(_real, name)
