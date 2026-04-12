"""Backward-compatibility shim — moved to bernstein.core.server.dashboard_auth."""

import importlib as _importlib

from bernstein.core.server.dashboard_auth import *  # noqa: F403

_real = _importlib.import_module("bernstein.core.server.dashboard_auth")


def __getattr__(name: str):
    return getattr(_real, name)
