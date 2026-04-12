"""Backward-compatibility shim — moved to bernstein.core.orchestration.orchestrator_recovery."""

import importlib as _importlib

from bernstein.core.orchestration.orchestrator_recovery import *  # noqa: F403

_real = _importlib.import_module("bernstein.core.orchestration.orchestrator_recovery")


def __getattr__(name: str):
    return getattr(_real, name)
