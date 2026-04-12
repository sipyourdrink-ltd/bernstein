"""Backward-compatibility shim — moved to bernstein.core.server.webhook_verify."""

import importlib as _importlib

from bernstein.core.server.webhook_verify import *  # noqa: F403

_real = _importlib.import_module("bernstein.core.server.webhook_verify")


def __getattr__(name: str):
    return getattr(_real, name)
