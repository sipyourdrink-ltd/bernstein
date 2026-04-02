"""Bernstein plugin system using pluggy."""

from __future__ import annotations

import pluggy
from typing import Any, Callable, TypeVar

F = TypeVar("F", bound=Callable[..., Any])

def hookspec(
    func: F | None = None,
    firstresult: bool = False,
    historic: bool = False,
    background: bool = False,
) -> Any:
    """Decorator to mark a function as a hook specification.

    Args:
        func: The function to decorate.
        firstresult: If True, the call will stop at the first non-None result.
        historic: If True, every call to this hook is remembered and will be
            replayed on new plugins.
        background: If True, the hook will be executed in the background
            without blocking the main orchestration pipeline.
    """
    marker = pluggy.HookspecMarker("bernstein")

    if func is not None:
        # Called as @hookspec
        res = marker(func)
        setattr(res, "bernstein_background", background)
        return res

    def decorator(func: F) -> F:
        # Called as @hookspec(firstresult=True, ...)
        res = marker(firstresult=firstresult, historic=historic)(func)
        setattr(res, "bernstein_background", background)
        return res

    return decorator

hookimpl = pluggy.HookimplMarker("bernstein")

__all__ = ["hookimpl", "hookspec"]
