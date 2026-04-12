"""Backward-compatibility shim — moved to bernstein.core.agents.claude_session_resume."""
from bernstein.core.agents.claude_session_resume import *  # noqa: F401,F403

import importlib as _importlib
_real = _importlib.import_module("bernstein.core.agents.claude_session_resume")
def __getattr__(name: str):
    return getattr(_real, name)
