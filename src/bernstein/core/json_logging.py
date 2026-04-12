"""Backward-compatibility shim — moved to bernstein.core.server.json_logging."""

import importlib as _importlib

from bernstein.core.server.json_logging import *  # noqa: F403

_real = _importlib.import_module("bernstein.core.server.json_logging")


def __getattr__(name: str):
    return getattr(_real, name)
