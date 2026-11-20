"""server sub-package - re-exports for backward compatibility.

Imports directly from subpackage modules (NOT parent-level shims)
to avoid circular imports through parent-level shim modules.
"""

from importlib import import_module as _import_module
from typing import TYPE_CHECKING as _TYPE_CHECKING
from typing import Any as _Any

if _TYPE_CHECKING:
    from bernstein.core.server.server_app import *  # noqa: F403
    from bernstein.core.server.server_middleware import *  # noqa: F403
    from bernstein.core.server.server_models import *  # noqa: F403

# Preserve the names (and last-writer precedence) of the historical three
# star imports. Importing a standalone helper under this package should
# not construct FastAPI's models or load HTTP routes in the launcher.
_EXPORT_MODULES = ("server_app", "server_middleware", "server_models")


def _star_names() -> list[str]:
    names = set(_EXPORT_MODULES)
    for source in _EXPORT_MODULES:
        module = _import_module(f"{__name__}.{source}")
        exported = getattr(module, "__all__", None)
        names.update(exported if exported is not None else (name for name in vars(module) if not name.startswith("_")))
    return sorted(names)


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(_star_names()))


def __getattr__(name: str) -> _Any:
    """Lazy fallback for attributes not eagerly exported.

    A module in the fallback chain that fails to import must not decide the
    answer for an unrelated attribute: the remaining modules are still
    consulted, and a genuine miss still raises ``AttributeError`` rather than
    leaking that module's ``ImportError``. The failures are named in the final
    message so a broken module stays visible instead of being swallowed.
    """
    import importlib

    if name == "__all__":
        return _star_names()
    if name.startswith("__"):
        raise AttributeError(name)
    # Python requests a package attribute before importing a named submodule
    # for "from package import submodule". Resolve a real child directly.
    import sys
    from importlib.util import find_spec

    child = f"{__name__}.{name}"
    if name.isidentifier() and (child in sys.modules or find_spec(child) is not None):
        return importlib.import_module(child)

    unimportable: list[str] = []
    for mod_name in (
        *(f"{__name__}.{source}" for source in reversed(_EXPORT_MODULES)),
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
