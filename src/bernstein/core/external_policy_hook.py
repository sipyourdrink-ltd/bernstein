"""Backward-compatibility shim — moved to bernstein.core.security.external_policy_hook."""

import importlib as _importlib

from bernstein.core.security.external_policy_hook import *  # noqa: F403

_real = _importlib.import_module("bernstein.core.security.external_policy_hook")


def __getattr__(name: str):
    return getattr(_real, name)
