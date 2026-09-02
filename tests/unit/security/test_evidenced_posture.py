"""The posture score is a projection of the chain, never of the configuration (#4989).

Every posture number in this space is derived from what is switched on, which
measures intent. This one is derived from what the chain evidences, so the only
way to raise it is to produce evidence.

Each test names the property it protects:

1. enabling a control without producing evidence leaves the document byte-identical;
2. producing evidence for that control raises the score;
3. the document names every contributing chain event;
4. the document names the weights version;
5. recomputing from the same chain is byte-identical;
6. the denominator counts what was measurable, not what exists;
7. nothing measurable reports an absent score rather than 0 or 100;
8. the signature covers the score, so an edited document does not verify;
9. the operator surface emits exactly the projected document.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from bernstein.cli.commands.governance_cmd import governance_group
from bernstein.core.compliance.coverage import (
    ControlCoverageResult,
    ControlCoverageStatus,
)
from bernstein.core.lineage.identity import AgentCard, generate_keypair
from bernstein.core.lineage.signed_write import SignedLineageLog
from bernstein.core.lineage.store import LineageStore
from bernstein.core.security.compliance_policies import (
    ComplianceFramework,
    CompliancePolicyLibrary,
)
from bernstein.core.security.security_posture import (
    POSTURE_WEIGHTS_VERSION,
    compute_evidenced_posture,
    evidenced_posture_json,
)

_KEY = b"0" * 32


def _record(workdir: Path, *paths: str) -> None:
    """Seal one signed lineage entry per artefact path into ``<workdir>/.sdd``."""
    store = LineageStore(workdir / ".sdd" / "lineage")
    recorder = SignedLineageLog(store=store, operator_hmac_key=_KEY)
    priv, pub = generate_keypair()
    card = AgentCard(agent_id="agent:worker", kid="k1", public_key_pem=pub)
    for index, path in enumerate(paths):
        recorder.record_write(
            artefact_path=path,
            new_content=path.encode(),
            agent_id=card.agent_id,
            agent_card=card,
            private_key_pem=priv,
            tool_call_id=f"tc-{index}",
            span_id=f"span-{index}",
            ts_ns=1_000 + index,
        )


def _document(workdir: Path) -> dict[str, object]:
    return json.loads(evidenced_posture_json(workdir, hmac_key=_KEY))


def _result(
    policy_id: str,
    status: ControlCoverageStatus,
    evidence_refs: tuple[str, ...] = (),
) -> ControlCoverageResult:
    return ControlCoverageResult(
        policy_id=policy_id,
        control_id=f"{policy_id}-control",
        status=status,
        evidence_summary="",
        missing_inputs=[],
        reason="",
        evidence_refs=evidence_refs,
    )


# 1 -- the load-bearing property: configuration is not an input.
def test_enabling_a_control_without_evidence_does_not_change_the_score(tmp_path: Path) -> None:
    _record(tmp_path, "task/t-1")
    before = evidenced_posture_json(tmp_path, hmac_key=_KEY)

    CompliancePolicyLibrary().enable(
        ComplianceFramework.SOC2,
        config_dir=tmp_path / ".sdd" / "compliance",
    )
    assert (tmp_path / ".sdd" / "compliance" / "enabled" / "soc2.yaml").exists()

    assert evidenced_posture_json(tmp_path, hmac_key=_KEY) == before


# 2 -- the only lever on the number is evidence.
def test_producing_evidence_for_a_control_raises_the_score(tmp_path: Path) -> None:
    _record(tmp_path, "task/t-1")
    before = _document(tmp_path)["score"]

    _record(tmp_path, "auth/login-1")
    after = _document(tmp_path)["score"]

    assert isinstance(before, float)
    assert isinstance(after, float)
    assert after > before


# 3 -- a number a third party cannot re-derive is not evidence.
def test_document_names_every_contributing_chain_event(tmp_path: Path) -> None:
    _record(tmp_path, "task/t-1", "auth/login-1")

    store = LineageStore(tmp_path / ".sdd" / "lineage")
    chain_hashes = {entry.content_hash for entry, _ in store.read_log()}
    assert chain_hashes

    document = _document(tmp_path)
    contributions = document["contributions"]
    assert isinstance(contributions, list)
    evidenced = [c for c in contributions if c["evidenced"]]
    assert evidenced
    for contribution in evidenced:
        assert contribution["chain_events"]
        assert set(contribution["chain_events"]) <= chain_hashes
    for contribution in contributions:
        if not contribution["evidenced"]:
            assert contribution["chain_events"] == []


# 4 -- a score is meaningless without the weights that produced it.
def test_document_names_the_weights_version(tmp_path: Path) -> None:
    _record(tmp_path, "task/t-1")
    document = _document(tmp_path)

    assert document["weights_version"] == POSTURE_WEIGHTS_VERSION
    for contribution in document["contributions"]:
        assert isinstance(contribution["weight"], float)


# 5 -- the number is a pure function of the chain.
def test_recomputing_from_the_same_chain_is_byte_identical(tmp_path: Path) -> None:
    _record(tmp_path, "task/t-1", "auth/login-1", "incident/i-1")

    first = evidenced_posture_json(tmp_path, hmac_key=_KEY)
    second = evidenced_posture_json(tmp_path, hmac_key=_KEY)

    assert first == second


# 6 -- the denominator is what was measurable, not what exists.
def test_denominator_counts_only_measurable_controls() -> None:
    posture = compute_evidenced_posture(
        [
            _result("evidenced-one", ControlCoverageStatus.EVIDENCED, ("sha256:a",)),
            _result("unevidenced-one", ControlCoverageStatus.PARTIALLY_EVIDENCED),
            _result("unmeasurable-one", ControlCoverageStatus.NOT_EVIDENCEABLE),
        ]
    )

    assert posture.registered_controls == 3
    assert posture.measurable_controls == 2
    assert posture.measurable_weight == pytest.approx(2.0)
    assert posture.evidenced_weight == pytest.approx(1.0)
    assert posture.score == pytest.approx(50.0)
    assert [c.control_id for c in posture.contributions] == [
        "evidenced-one-control",
        "unevidenced-one-control",
    ]


# 7 -- zero over zero is absent evidence, not a perfect score.
def test_absent_score_when_nothing_was_measurable() -> None:
    posture = compute_evidenced_posture([_result("unmeasurable-one", ControlCoverageStatus.NOT_EVIDENCEABLE)])

    assert posture.score is None
    assert posture.measurable_controls == 0
    assert posture.registered_controls == 1


# 8 -- the signature binds the score to the chain key.
def test_signature_covers_the_score(tmp_path: Path) -> None:
    _record(tmp_path, "task/t-1")
    document = _document(tmp_path)

    signature = document.pop("signature")
    body = json.dumps(document, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
    assert signature == hmac.new(_KEY, body, hashlib.sha256).hexdigest()

    document["score"] = 100.0
    forged = json.dumps(document, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
    assert signature != hmac.new(_KEY, forged, hashlib.sha256).hexdigest()


# 9 -- the operator surface emits exactly the projected document.
def test_governance_posture_command_emits_the_projected_document(tmp_path: Path) -> None:
    _record(tmp_path, "task/t-1", "auth/login-1")
    key_path = tmp_path / "audit.key"
    key_path.write_bytes(_KEY)
    key_path.chmod(0o600)

    result = CliRunner().invoke(
        governance_group,
        ["posture", "--workdir", str(tmp_path), "--json-output"],
        env={"BERNSTEIN_AUDIT_KEY_PATH": str(key_path)},
    )

    assert result.exit_code == 0, result.output
    assert result.output.strip() == evidenced_posture_json(tmp_path, hmac_key=_KEY)
