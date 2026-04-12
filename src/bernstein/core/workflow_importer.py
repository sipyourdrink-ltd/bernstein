"""Backward-compatibility shim — moved to bernstein.core.planning.workflow_importer."""
from bernstein.core.planning.workflow_importer import *  # noqa: F401,F403

import importlib as _importlib
_real = _importlib.import_module("bernstein.core.planning.workflow_importer")
def __getattr__(name: str):
    return getattr(_real, name)
