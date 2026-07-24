"""``bernstein worker --adapter`` agrees with the seed and ``run --cli`` (issue #2807).

The worker's adapter allowlist was a stale hardcoded subset, so a cluster
worker could not run adapters (e.g. ``opencode``) that the seed ``cli:`` key and
``run --cli`` accept. All three now derive from ``selectable_adapter_names`` via
``adapter_cli_choice`` so a given adapter resolves identically everywhere.
"""

from __future__ import annotations

import click

from bernstein.cli.commands.worker_cmd import worker
from bernstein.cli.run_bootstrap import run


def _choices(cmd: click.Command, opt_name: str) -> set[str]:
    for param in cmd.params:
        if param.name == opt_name and isinstance(param.type, click.Choice):
            return set(param.type.choices)
    raise AssertionError(f"{opt_name} choice option not found on {cmd.name}")


def test_worker_adapter_matches_run_cli() -> None:
    assert _choices(worker, "adapter") == _choices(run, "cli")


def test_worker_adapter_includes_registered_adapter_rejects_mock() -> None:
    choices = _choices(worker, "adapter")
    assert "opencode" in choices  # previously missing from the worker subset
    assert "mock" not in choices  # non-agent stub, rejected everywhere
