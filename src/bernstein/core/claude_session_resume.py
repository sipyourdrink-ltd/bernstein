"""Backward-compatibility shim — moved to bernstein.core.agents.claude_session_resume."""

import importlib as _importlib

from bernstein.core.agents.claude_session_resume import *  # noqa: F403

_real = _importlib.import_module("bernstein.core.agents.claude_session_resume")


def __getattr__(name: str):
    return getattr(_real, name)
