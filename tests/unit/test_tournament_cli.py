"""CLI surface for tournament selection receipts (issue #2353).

``bernstein tournament show`` renders a receipt and ``bernstein tournament
verify`` recomputes it offline. Both are isolated from the developer's real
state via ``BERNSTEIN_AUDIT_KEY_PATH`` and an explicit ``--workdir`` so the
tests are cwd-independent.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from bernstein.core.security.audit import load_or_create_audit_key
from bernstein.core.tournament.evaluators import AttemptOutcome, EvaluatorOutput
from bernstein.core.tournament.receipt import (
    emit_tournament_receipt,
    load_or_create_tournament_identity,
)
from bernstein.core.tournament.spec import EvaluatorSpec, TournamentSpec


def _seed(workdir: Path) -> None:
    hmac_key = load_or_create_audit_key(workdir / ".sdd" / "audit.key")
    priv, pub = load_or_create_tournament_identity(workdir / ".sdd" / "tournaments")
    spec = TournamentSpec(
        attempts=2,
        evaluators=(EvaluatorSpec(name="tests", weight=1.0),),
    )
    outcomes = [
        AttemptOutcome.from_output_bytes(b"win", outputs=(EvaluatorOutput(name="tests", value=1.0),)),
        AttemptOutcome.from_output_bytes(b"lose", outputs=(EvaluatorOutput(name="tests", value=0.0),)),
    ]
    emit_tournament_receipt(
        workdir=workdir,
        lineage_root=workdir / ".sdd" / "lineage",
        hmac_key=hmac_key,
        private_key_pem=priv,
        public_key_pem=pub,
        task_id="T-cli",
        spec=spec,
        outcomes=outcomes,
        timestamp=100,
    )


@pytest.fixture
def _seeded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("BERNSTEIN_AUDIT_KEY_PATH", str(tmp_path / ".sdd" / "audit.key"))
    _seed(tmp_path)
    return tmp_path


def test_show_renders_receipt(_seeded: Path) -> None:
    from bernstein.cli.main import cli

    result = CliRunner().invoke(cli, ["tournament", "show", "T-cli", "--workdir", str(_seeded)])
    assert result.exit_code == 0, result.output
    assert "Tournament selection" in result.output
    assert "chosen" in result.output


def test_show_missing_receipt_exits_1(tmp_path: Path) -> None:
    from bernstein.cli.main import cli

    (tmp_path / ".sdd").mkdir()
    result = CliRunner().invoke(cli, ["tournament", "show", "nope", "--workdir", str(tmp_path)])
    assert result.exit_code == 1


def test_verify_passes(_seeded: Path) -> None:
    from bernstein.cli.main import cli

    result = CliRunner().invoke(cli, ["tournament", "verify", "T-cli", "--workdir", str(_seeded)])
    assert result.exit_code == 0, result.output
    assert "OK" in result.output


def test_verify_missing_exits_1(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from bernstein.cli.main import cli

    monkeypatch.setenv("BERNSTEIN_AUDIT_KEY_PATH", str(tmp_path / ".sdd" / "audit.key"))
    (tmp_path / ".sdd").mkdir()
    result = CliRunner().invoke(cli, ["tournament", "verify", "nope", "--workdir", str(tmp_path)])
    assert result.exit_code == 1
