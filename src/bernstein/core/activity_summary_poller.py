"""Backward-compatibility shim — moved to bernstein.core.orchestration.activity_summary_poller."""

import importlib as _importlib

from bernstein.core.orchestration.activity_summary_poller import *  # noqa: F403

_real = _importlib.import_module("bernstein.core.orchestration.activity_summary_poller")


def __getattr__(name: str):
    return getattr(_real, name)
