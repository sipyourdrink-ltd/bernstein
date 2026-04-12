"""Backward-compatibility shim — moved to bernstein.core.security.policy_limits."""

import importlib as _importlib

from bernstein.core.security.policy_limits import *  # noqa: F403

_real = _importlib.import_module("bernstein.core.security.policy_limits")


def __getattr__(name: str):
    return getattr(_real, name)
