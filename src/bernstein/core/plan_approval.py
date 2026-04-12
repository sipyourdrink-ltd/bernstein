"""Backward-compatibility shim — moved to bernstein.core.security.plan_approval."""

import importlib as _importlib

from bernstein.core.security.plan_approval import *  # noqa: F403

_real = _importlib.import_module("bernstein.core.security.plan_approval")


def __getattr__(name: str):
    return getattr(_real, name)
