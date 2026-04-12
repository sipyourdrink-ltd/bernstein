"""Backward-compatibility shim — moved to bernstein.core.orchestration.process_utils."""
import importlib as _importlib

from bernstein.core.orchestration.process_utils import *  # noqa: F403

_real = _importlib.import_module("bernstein.core.orchestration.process_utils")
def __getattr__(name: str):
    return getattr(_real, name)
