"""Backward-compatibility shim — moved to bernstein.core.server.request_dedup."""

import importlib as _importlib

from bernstein.core.server.request_dedup import *  # noqa: F403

_real = _importlib.import_module("bernstein.core.server.request_dedup")


def __getattr__(name: str):
    return getattr(_real, name)
