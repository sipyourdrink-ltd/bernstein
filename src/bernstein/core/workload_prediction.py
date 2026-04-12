"""Backward-compatibility shim — moved to bernstein.core.orchestration.workload_prediction."""

import importlib as _importlib

from bernstein.core.orchestration.workload_prediction import *  # noqa: F403

_real = _importlib.import_module("bernstein.core.orchestration.workload_prediction")


def __getattr__(name: str):
    return getattr(_real, name)
