"""Backward-compat shim for bernstein.core.tokens.claude_prompt_cache_optimizer."""

from bernstein.core._shim import install_shim

install_shim(__name__, "bernstein.core.tokens.claude_prompt_cache_optimizer")
