"""Backward-compatibility shim — moved to bernstein.core.server.http_retry."""

import importlib as _importlib

from bernstein.core.server.http_retry import *  # noqa: F403

_real = _importlib.import_module("bernstein.core.server.http_retry")


def __getattr__(name: str):
    return getattr(_real, name)
