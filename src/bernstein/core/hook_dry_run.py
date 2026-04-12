"""Backward-compatibility shim — moved to bernstein.core.config.hook_dry_run."""
from bernstein.core.config.hook_dry_run import *  # noqa: F401,F403

import importlib as _importlib
_real = _importlib.import_module("bernstein.core.config.hook_dry_run")
def __getattr__(name: str):
    return getattr(_real, name)
