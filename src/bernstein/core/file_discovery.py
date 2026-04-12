"""Backward-compatibility shim — moved to bernstein.core.knowledge.file_discovery."""

import importlib as _importlib

from bernstein.core.knowledge.file_discovery import *  # noqa: F403

_real = _importlib.import_module("bernstein.core.knowledge.file_discovery")


def __getattr__(name: str):
    return getattr(_real, name)
