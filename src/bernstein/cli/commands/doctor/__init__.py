"""Observability doctor subcommands for Bernstein.

This package collects the per-backend ``bernstein doctor`` probes for
the operator-facing observability surface.

Currently registered subcommands:

* ``bernstein doctor code-scanning`` -- GitHub Code Scanning alerts by
  severity for the current repository.
* ``bernstein doctor observe`` -- umbrella that runs every backend
  probe (Code Scanning) and renders one aggregated table. Supports
  ``--json`` and ``--watch``.

Each per-backend module exposes a ``register(parent_group)`` helper
that the CLI bootstrap calls from
:mod:`bernstein.cli.commands.advanced_cmd` (and re-asserted from
:mod:`bernstein.cli.main`) so wiring is explicit and side-effect-free
outside the doctor group.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from bernstein.cli.commands.doctor.backends import (
    BackendReport,
    MetricRow,
    ProbeStatus,
    apply_deltas,
    load_previous,
    probe_code_scanning,
    save_snapshot,
)
from bernstein.cli.commands.doctor.code_scanning import (
    code_scanning_cmd,
)
from bernstein.cli.commands.doctor.code_scanning import (
    register as register_code_scanning,
)
from bernstein.cli.commands.doctor.observe import (
    observe_cmd,
)
from bernstein.cli.commands.doctor.observe import (
    register as register_observe,
)

if TYPE_CHECKING:
    import click

__all__ = [
    "BackendReport",
    "MetricRow",
    "ProbeStatus",
    "apply_deltas",
    "code_scanning_cmd",
    "load_previous",
    "observe_cmd",
    "probe_code_scanning",
    "register_code_scanning",
    "register_observe",
    "save_snapshot",
]


def register_all(parent_group: click.Group) -> None:
    """Attach every per-backend subcommand to a Click group."""

    register_code_scanning(parent_group)
    register_observe(parent_group)
