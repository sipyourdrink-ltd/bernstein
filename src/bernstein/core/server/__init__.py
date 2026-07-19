"""server sub-package - re-exports for backward compatibility.

Imports directly from subpackage modules (NOT parent-level shims)
to avoid circular imports through parent-level shim modules.
"""

from typing import Any as _Any

from bernstein.core.server.server_app import *  # noqa: F403
from bernstein.core.server.server_middleware import *  # noqa: F403
from bernstein.core.server.server_models import *  # noqa: F403


def __getattr__(name: str) -> _Any:
    """Lazy fallback for attributes not eagerly exported.

    A module in the fallback chain that fails to import must not decide the
    answer for an unrelated attribute: the remaining modules are still
    consulted, and a genuine miss still raises ``AttributeError`` rather than
    leaking that module's ``ImportError``. The failures are named in the final
    message so a broken module stays visible instead of being swallowed.
    """
    import importlib

    unimportable: list[str] = []
    for mod_name in (
        "bernstein.core.server.server_app",
        "bernstein.core.server.server_models",
        "bernstein.core.server.server_middleware",
        "bernstein.core.server.server",
    ):
        try:
            mod = importlib.import_module(mod_name)
        except ImportError as exc:
            unimportable.append(f"{mod_name} ({exc})")
            continue
        try:
            return getattr(mod, name)
        except AttributeError:
            continue
    msg = f"module {__name__!r} has no attribute {name!r}"
    if unimportable:
        msg += "; these fallback modules could not be imported: " + ", ".join(unimportable)
    raise AttributeError(msg)
