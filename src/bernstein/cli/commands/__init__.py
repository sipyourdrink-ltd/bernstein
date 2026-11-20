"""commands sub-package.

Re-exports the ``adapters`` Click group so callers can do::

    from bernstein.cli.commands import adapters_group

without depending on the historical layout under ``adapter_cmd``.
"""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from click import Command

    from bernstein.cli.commands.adapter_cmd import adapters_group
    from bernstein.cli.commands.adapters_cmd import adapters_check_cmd, adapters_list_status_cmd

__all__ = [
    "adapters_check_cmd",
    "adapters_group",
    "adapters_list_status_cmd",
]


def __getattr__(name: str) -> Command:
    if name not in __all__:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module = ".adapter_cmd" if name == "adapters_group" else ".adapters_cmd"
    return cast("Command", getattr(import_module(module, __name__), name))


def __dir__() -> list[str]:
    return sorted(globals().keys() | set(__all__))
