"""Backward-compatibility shim — moved to bernstein.core.security.policy_templates."""

import importlib as _importlib

from bernstein.core.security.policy_templates import *  # noqa: F403

_real = _importlib.import_module("bernstein.core.security.policy_templates")


def __getattr__(name: str):
    return getattr(_real, name)
