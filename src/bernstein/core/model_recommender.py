"""Backward-compatibility shim — moved to bernstein.core.routing.model_recommender."""

from bernstein.core._shim import install_shim

install_shim(__name__, "bernstein.core.routing.model_recommender")
