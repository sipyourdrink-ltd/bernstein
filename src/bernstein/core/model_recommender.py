"""Backward-compatibility shim — moved to bernstein.core.routing.model_recommender."""

import importlib as _importlib

from bernstein.core.routing.model_recommender import *  # noqa: F403

_real = _importlib.import_module("bernstein.core.routing.model_recommender")


def __getattr__(name: str):
    return getattr(_real, name)
