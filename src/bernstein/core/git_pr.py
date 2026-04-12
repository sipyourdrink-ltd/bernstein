"""Backward-compatibility shim — moved to bernstein.core.git.git_pr."""
from bernstein.core.git.git_pr import *  # noqa: F401,F403

import importlib as _importlib
_real = _importlib.import_module("bernstein.core.git.git_pr")
def __getattr__(name: str):
    return getattr(_real, name)
