"""Bernstein plugin system using pluggy."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

import pluggy


class _QualityGate(ABC):
    """Identify a quality gate without importing the quality runner."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Unique gate name."""


def hookspec(
    func: Callable[..., Any] | None = None,
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
        setattr(res, "bernstein_background", background)  # noqa: B010
        return res

    def decorator(f: Callable[..., Any]) -> Any:
        # Called as @hookspec(firstresult=True, ...)
        res = marker(firstresult=firstresult, historic=historic)(f)
        setattr(res, "bernstein_background", background)  # noqa: B010
        return res

    return decorator


hookimpl = pluggy.HookimplMarker("bernstein")

__all__ = ["hookimpl", "hookspec"]
