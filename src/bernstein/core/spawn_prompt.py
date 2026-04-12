"""Backward-compatibility shim — moved to bernstein.core.agents.spawn_prompt."""

from bernstein.core._shim import install_shim

install_shim(__name__, "bernstein.core.agents.spawn_prompt")
