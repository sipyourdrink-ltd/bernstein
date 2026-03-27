"""Bernstein plugin system using pluggy."""

from __future__ import annotations

import pluggy

hookspec = pluggy.HookspecMarker("bernstein")
hookimpl = pluggy.HookimplMarker("bernstein")

__all__ = ["hookimpl", "hookspec"]
