"""server sub-package — re-exports for backward compatibility.

Imports directly from subpackage modules (NOT parent-level shims)
to avoid circular imports through parent-level shim modules.
"""

from typing import Any as _Any

from bernstein.core.server.server_app import *  # noqa: F403
from bernstein.core.server.server_middleware import *  # noqa: F403
from bernstein.core.server.server_models import *  # noqa: F403


def __getattr__(name: str) -> _Any:
    """Lazy fallback for attributes not eagerly exported."""
    import importlib

    for mod_name in (
        "bernstein.core.server.server_app",
        "bernstein.core.server.server_models",
        "bernstein.core.server.server_middleware",
        "bernstein.core.server.server",
    ):
        mod = importlib.import_module(mod_name)
        try:
            return getattr(mod, name)
        except AttributeError:
            continue
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
