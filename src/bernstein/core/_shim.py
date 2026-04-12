"""Backward-compatibility shim generator.

Provides ``install_shim()`` which replaces the calling module's namespace
with a thin delegation layer to the real target module.  This lets ~240
shim files collapse from 5-12 lines of duplicated boilerplate to a single
call, eliminating SonarCloud code-duplication alerts.

Usage (in a shim file)::

    \"\"\"Backward-compatibility shim -- moved to bernstein.core.agents.spawner.\"\"\"
    from bernstein.core._shim import install_shim; install_shim(__name__, "bernstein.core.agents.spawner")  # noqa: E702
"""

from __future__ import annotations

import importlib
import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from types import ModuleType


def install_shim(caller_name: str, target: str) -> None:
    """Replace *caller_name*'s module namespace with a delegation to *target*.

    1. Eagerly copies every public attribute from *target* so that
       ``from bernstein.core.X import SomeClass`` works.
    2. Installs a module-level ``__getattr__`` so that private names
       (``_helper``, ``__all__``, etc.) are resolved lazily from *target*.

    Must be called at module level::

        install_shim(__name__, "bernstein.core.subpkg.module")

    Args:
        caller_name: ``__name__`` of the shim module (the one being replaced).
        target: Fully-qualified name of the real module to delegate to.
    """
    real: ModuleType = importlib.import_module(target)
    caller: ModuleType = sys.modules[caller_name]

    # Eagerly copy public symbols for ``from X import Y`` support.
    for attr in dir(real):
        if not attr.startswith("_"):
            setattr(caller, attr, getattr(real, attr))

    # Forward __all__ if the target defines one.
    target_all: list[str] | None = getattr(real, "__all__", None)
    if target_all is not None:
        caller.__all__ = target_all  # type: ignore[attr-defined]

    # Lazy fallback for private names and anything added after import.
    def _getattr(name: str) -> object:
        try:
            return getattr(real, name)
        except AttributeError:
            raise AttributeError(f"module {caller_name!r} has no attribute {name!r}") from None

    caller.__getattr__ = _getattr  # type: ignore[attr-defined]
