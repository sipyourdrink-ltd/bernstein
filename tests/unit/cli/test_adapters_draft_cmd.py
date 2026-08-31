"""Tests for ``bernstein adapters draft`` (remaining work on issue #3763).

``draft_from_evidence`` (from an earlier slice) had no caller reachable
outside its own test file, no YAML persistence, and no operator
confirmation. These tests cover the CLI surface that closes those three
gaps: a real probe subprocess, a real confirmation gate, and a real file
written only when the operator accepts.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from bernstein.adapters.draft import read_draft_document
from bernstein.cli.commands.adapters_draft_cmd import _execute_draft
from bernstein.cli.main import cli

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "probe"
RECORDABLE_FIXTURE = FIXTURES / "probe_recordable.py"
NO_MODEL_FIXTURE = FIXTURES / "probe_ok.py"


# ---------------------------------------------------------------------------
# _execute_draft: the full pipeline (real probe subprocess, real file),
# confirmation injected so no test depends on a TTY.
# ---------------------------------------------------------------------------


def test_execute_draft_confirmed_persists_a_real_file_showing_the_exact_argv(tmp_path: Path) -> None:
    """A confirmed draft persists YAML to out_dir, and the operator saw the real argv first."""
    seen_previews: list[str] = []

    def confirm(preview: str) -> bool:
        seen_previews.append(preview)
        return True

    rc, target = _execute_draft(
        str(RECORDABLE_FIXTURE),
        evidence_dir=tmp_path / "evidence",
        out_dir=tmp_path / "drafts",
        required_fields=set(),
        preview_model="fixture-model",
        preview_prompt="fixture-prompt",
        assume_yes=False,
        confirm=confirm,
    )

    assert rc == 0
    assert target is not None and target.is_file()

    # The confirmation the operator actually saw named the exact argv,
    # binary included - not a placeholder or a summary.
    assert len(seen_previews) == 1
    assert str(RECORDABLE_FIXTURE) in seen_previews[0]
    assert "--model fixture-model" in seen_previews[0]
    assert "--prompt fixture-prompt" in seen_previews[0]

    document = read_draft_document(target)
    assert document["invocation"]["model_flag"] == "--model"


def test_execute_draft_declined_leaves_out_dir_untouched(tmp_path: Path) -> None:
    """Declining the confirmation writes nothing - out_dir is never even created."""
    out_dir = tmp_path / "drafts"

    rc, target = _execute_draft(
        str(RECORDABLE_FIXTURE),
        evidence_dir=tmp_path / "evidence",
        out_dir=out_dir,
        required_fields=set(),
        preview_model="m",
        preview_prompt="p",
        assume_yes=False,
        confirm=lambda _preview: False,
    )

    assert rc == 1
    assert target is None
    assert not out_dir.exists(), "a declined draft must not create out_dir"


def test_execute_draft_yes_flag_never_calls_the_confirmation_gate(tmp_path: Path) -> None:
    """--yes bypasses confirmation entirely, not merely auto-answers it."""

    def confirm(_preview: str) -> bool:
        raise AssertionError("confirm() must not be called when assume_yes=True")

    rc, target = _execute_draft(
        str(RECORDABLE_FIXTURE),
        evidence_dir=tmp_path / "evidence",
        out_dir=tmp_path / "drafts",
        required_fields=set(),
        preview_model="m",
        preview_prompt="p",
        assume_yes=True,
        confirm=confirm,
    )

    assert rc == 0
    assert target is not None and target.is_file()


def test_execute_draft_refuses_missing_required_field_without_ever_confirming(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A --require'd field missing from a real probe refuses before confirmation, writes nothing."""
    out_dir = tmp_path / "drafts"

    def confirm(_preview: str) -> bool:
        raise AssertionError("confirm() must not be called when drafting itself refused")

    rc, target = _execute_draft(
        str(NO_MODEL_FIXTURE),
        evidence_dir=tmp_path / "evidence",
        out_dir=out_dir,
        required_fields={"model_flag"},
        preview_model="m",
        preview_prompt="p",
        assume_yes=False,
        confirm=confirm,
    )

    assert rc == 2
    assert target is None
    assert not out_dir.exists()
    assert "--model" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# The actual `bernstein adapters draft` command surface, end to end.
# ---------------------------------------------------------------------------


def test_adapters_draft_cli_confirmed_via_stdin_writes_the_draft(tmp_path: Path) -> None:
    """The registered CLI command, invoked the way an operator would, persists on 'y'."""
    out_dir = tmp_path / "drafts"
    runner = CliRunner()

    result = runner.invoke(
        cli,
        [
            "adapters",
            "draft",
            str(RECORDABLE_FIXTURE),
            "--evidence-dir",
            str(tmp_path / "evidence"),
            "--out-dir",
            str(out_dir),
        ],
        input="y\n",
    )

    assert result.exit_code == 0, result.output
    assert "Wrote drafted profile to" in result.output
    written = list(out_dir.glob("*.yaml"))
    assert len(written) == 1, written


def test_adapters_draft_cli_declined_via_stdin_writes_nothing(tmp_path: Path) -> None:
    """The registered CLI command exits 1 and writes nothing when the operator answers 'n'."""
    out_dir = tmp_path / "drafts"
    runner = CliRunner()

    result = runner.invoke(
        cli,
        [
            "adapters",
            "draft",
            str(RECORDABLE_FIXTURE),
            "--evidence-dir",
            str(tmp_path / "evidence"),
            "--out-dir",
            str(out_dir),
        ],
        input="n\n",
    )

    assert result.exit_code == 1
    assert not out_dir.exists()
