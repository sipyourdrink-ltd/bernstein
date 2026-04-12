"""Backward-compatibility shim — moved to bernstein.core.security.external_policy_hook."""
from bernstein.core.security.external_policy_hook import *  # noqa: F401,F403

import importlib as _importlib
_real = _importlib.import_module("bernstein.core.security.external_policy_hook")
def __getattr__(name: str):
    return getattr(_real, name)
