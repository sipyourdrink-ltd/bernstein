"""CLI tests for ``bernstein verify ladder`` (#2927).

The subcommand re-derives the ladder receipt offline: per-tier
``tier / config_hash / evidence_hash / verdict`` plus the composite result,
non-zero exit on any re-derivation mismatch. Hermetic: the receipt is built
directly against a tmp project with the project's own audit key.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from bernstein.cli.commands.verify_cmd import verify_cmd
from bernstein.core.quality.verifier_ladder import (
    LadderReceipt,
    TierRecord,
    VerifierTier,
    build_ladder_receipt,
    canonical_hash,
    ladder_receipt_path,
    recompute_ladder_receipt_hash,
)
from bernstein.core.security.audit import load_or_create_audit_key

_TS = 1_700_000_000


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("BERNSTEIN_AUDIT_KEY_PATH", str(tmp_path / "audit.key"))
    (tmp_path / ".sdd").mkdir(parents=True, exist_ok=True)
    return tmp_path


def _rec(tier: VerifierTier, verdict: str = "pass") -> TierRecord:
    return TierRecord(
        tier=tier,
        config_hash=canonical_hash({"config": tier.value}),
        inputs_hash=canonical_hash({"inputs": "attributed-diff"}),
        evidence_hash=canonical_hash({"evidence": tier.value, "verdict": verdict}),
        verdict=verdict,
    )


def _build(project: Path, records: list[TierRecord]) -> LadderReceipt:
    key = load_or_create_audit_key(project / "audit.key")
    return build_ladder_receipt(
        task_id="T-001",
        records=records,
        workdir=project,
        lineage_root=project / ".sdd" / "lineage",
        hmac_key=key,
        timestamp=_TS,
    )


def test_verify_ladder_ok_exit_0(project: Path) -> None:
    receipt = _build(project, [_rec(VerifierTier.DETERMINISTIC), _rec(VerifierTier.JUDGE)])
    runner = CliRunner()
    result = runner.invoke(verify_cmd, ["ladder", receipt.receipt_hash, "-w", str(project)])
    assert result.exit_code == 0, result.output
    assert "deterministic" in result.output
    assert "judge" in result.output
    assert "VERIFIED" in result.output


def test_verify_ladder_missing_receipt_exit_1(project: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(verify_cmd, ["ladder", "sha256:" + "a" * 64, "-w", str(project)])
    assert result.exit_code == 1, result.output
    assert "no ladder receipt" in result.output


def test_verify_ladder_forged_merge_eligible_exit_2(project: Path) -> None:
    receipt = _build(project, [_rec(VerifierTier.DETERMINISTIC), _rec(VerifierTier.JUDGE, "fail")])
    assert receipt.merge_eligible is False

    path = ladder_receipt_path(project, receipt.receipt_hash)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["merge_eligible"] = True
    payload["receipt_hash"] = recompute_ladder_receipt_hash(payload)
    forged = ladder_receipt_path(project, payload["receipt_hash"])
    forged.write_text(json.dumps(payload), encoding="utf-8")

    runner = CliRunner()
    result = runner.invoke(verify_cmd, ["ladder", payload["receipt_hash"], "-w", str(project)])
    assert result.exit_code == 2, result.output
    assert "entail" in result.output or "re-deriv" in result.output
