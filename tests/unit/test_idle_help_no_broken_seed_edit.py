"""``run --idle`` help no longer tells the operator to make a crashing seed edit.

The ``--idle`` help instructed pinning ``cli: mock`` in bernstein.yaml, which a
real run rejects with ``SeedError`` (``mock`` is a non-selectable stub). ``--idle``
already forces the mock backend internally, so the instruction was both
unnecessary and actively broken (issue #2807).
"""

from __future__ import annotations

import click

from bernstein.cli.run_bootstrap import run


def _idle_help() -> str:
    for param in run.params:
        if param.name == "idle" and isinstance(param, click.Option):
            return param.help or ""
    raise AssertionError("--idle option not found on run")


def test_idle_help_drops_cli_mock_instruction() -> None:
    help_text = _idle_help()
    assert "cli: mock" not in help_text
    assert "pin" not in help_text.lower()


def test_idle_help_states_mock_is_forced_internally() -> None:
    help_text = _idle_help().lower()
    assert "internal" in help_text or "no bernstein.yaml edit" in help_text
