"""Backward-compatibility shim — moved to bernstein.core.security.policy_templates."""
from bernstein.core.security.policy_templates import *  # noqa: F401,F403

import importlib as _importlib
_real = _importlib.import_module("bernstein.core.security.policy_templates")
def __getattr__(name: str):
    return getattr(_real, name)
