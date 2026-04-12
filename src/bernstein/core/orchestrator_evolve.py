"""Backward-compatibility shim — moved to bernstein.core.orchestration.orchestrator_evolve."""

import importlib as _importlib

from bernstein.core.orchestration.orchestrator_evolve import *  # noqa: F403

_real = _importlib.import_module("bernstein.core.orchestration.orchestrator_evolve")


def __getattr__(name: str):
    return getattr(_real, name)
