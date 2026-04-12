"""Backward-compatibility shim — moved to bernstein.core.git.jira_sync."""
from bernstein.core.git.jira_sync import *  # noqa: F401,F403

import importlib as _importlib
_real = _importlib.import_module("bernstein.core.git.jira_sync")
def __getattr__(name: str):
    return getattr(_real, name)
