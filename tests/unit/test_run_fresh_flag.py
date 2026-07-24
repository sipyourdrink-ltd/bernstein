"""``bernstein run --fresh`` parity with the top-level help (issue #2800).

Top-level help advertises ``--fresh`` (``ignore saved session, start clean``),
but the ``run`` subcommand did not define it, so ``bernstein run --fresh``
failed with ``No such option``. These tests pin that the flag is accepted on
the ``run`` path and threaded through to the bootstrap ``force_fresh`` handling.
"""

from __future__ import annotations

from typing import Any

from click.testing import CliRunner

from bernstein.cli import run_bootstrap


def test_run_accepts_and_threads_fresh(monkeypatch: Any) -> None:
    """``run --fresh`` parses and threads ``force_fresh=True`` into the impl."""
    captured: dict[str, Any] = {}

    def fake_impl(**kwargs: Any) -> None:
        captured.update(kwargs)

    monkeypatch.setattr(run_bootstrap, "_run_impl", fake_impl)

    result = CliRunner().invoke(run_bootstrap.run, ["--fresh", "--goal", "hello", "--auto-approve"])

    assert result.exit_code == 0, result.output
    assert captured["force_fresh"] is True


def test_run_fresh_defaults_false(monkeypatch: Any) -> None:
    """Without ``--fresh`` the run defaults to resuming (``force_fresh=False``)."""
    captured: dict[str, Any] = {}

    def fake_impl(**kwargs: Any) -> None:
        captured.update(kwargs)

    monkeypatch.setattr(run_bootstrap, "_run_impl", fake_impl)

    result = CliRunner().invoke(run_bootstrap.run, ["--goal", "hello", "--auto-approve"])

    assert result.exit_code == 0, result.output
    assert captured["force_fresh"] is False
