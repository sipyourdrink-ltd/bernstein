"""Figure anchors resolve against the signed lineage log (issue #2888).

A figure is only as good as its receipt. These tests wire real lineage records
as anchor targets and prove:

* a figure anchored to a verifying record resolves with a provenance statement;
* an anchor to a *missing* record fails (the fabricated-figure case);
* an anchor to a *tampered* record fails at the signature (AC2 tamper test);
* the reserved ``receipt`` anchor kind (issue #2887) fails closed until wired.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bernstein.core.lineage.artifact_record import record_artifact
from bernstein.core.lineage.figure_grounding import LineageAnchorResolver, verify_report_figures
from bernstein.core.lineage.identity import AgentCard, generate_keypair
from bernstein.core.lineage.recorder import LineageRecorder
from bernstein.core.lineage.store import LineageStore
from bernstein.core.tasks.artifacts import ArtifactKind
from bernstein.core.tasks.figures import (
    Figure,
    FigureAnchor,
    ReportBundle,
    canonicalise_report_bundle,
)

_HMAC = b"k" * 64


@pytest.fixture
def identity() -> tuple[AgentCard, str]:
    priv, pub = generate_keypair()
    return AgentCard(agent_id="agent:analyst", kid="key-fig-001", public_key_pem=pub), priv


def _write_card(cards_dir: Path, card: AgentCard) -> None:
    d = cards_dir / card.agent_id
    d.mkdir(parents=True, exist_ok=True)
    (d / "card.json").write_text(
        json.dumps({"agent_id": card.agent_id, "kid": card.kid, "public_key_pem": card.public_key_pem}),
        encoding="utf-8",
    )


def _seed_source(tmp_path: Path, card: AgentCard, priv: str, rows: object) -> tuple[str, Path, Path]:
    """Record a source dataset artifact; return (its content_hash, log_path, cards_dir)."""
    log_root = tmp_path / "lineage"
    cards_dir = tmp_path / "cards"
    receipt = record_artifact(
        recorder=LineageRecorder(store=LineageStore(log_root), operator_hmac_key=_HMAC),
        sink_root=tmp_path / "artifacts",
        task_id="SRC-1",
        kind=ArtifactKind.DATASET,
        artifact=rows,
        agent_id=card.agent_id,
        agent_card=card,
        private_key_pem=priv,
    )
    _write_card(cards_dir, card)
    return receipt.content_hash, log_root / "log.jsonl", cards_dir


def _report(body: str, anchor_ref: str, value: str = "1,234") -> bytes:
    fig = Figure(value=value, unit="users", label="migrated users", anchor=FigureAnchor("artifact", anchor_ref))
    return canonicalise_report_bundle(ReportBundle(body=body, figures=(fig,)))


def test_figure_anchored_to_verifying_record_resolves(tmp_path: Path, identity: tuple[AgentCard, str]) -> None:
    card, priv = identity
    src_hash, log_path, cards_dir = _seed_source(tmp_path, card, priv, [{"id": 1}, {"id": 2}])

    verdict = verify_report_figures(
        canonical_bytes=_report("We migrated 1,234 users.\n", src_hash),
        log_path=log_path,
        cards_dir=cards_dir,
        operator_secret=_HMAC,
    )
    assert verdict.ok, verdict.failures
    assert verdict.provenances[0].ok
    assert "traces to artifact sha256:" in verdict.provenances[0].statement
    assert "chain position" in verdict.provenances[0].statement


def test_anchor_to_missing_record_fails(tmp_path: Path, identity: tuple[AgentCard, str]) -> None:
    card, priv = identity
    _src_hash, log_path, cards_dir = _seed_source(tmp_path, card, priv, [{"id": 1}])
    ghost = "sha256:" + "0" * 64

    verdict = verify_report_figures(
        canonical_bytes=_report("We migrated 1,234 users.\n", ghost),
        log_path=log_path,
        cards_dir=cards_dir,
        operator_secret=_HMAC,
    )
    assert not verdict.ok
    assert any("resolves to no lineage record" in f for f in verdict.failures)


def test_anchor_to_tampered_record_fails_at_signature(tmp_path: Path, identity: tuple[AgentCard, str]) -> None:
    """AC2: mutating the anchored record makes the figure fail verification."""
    card, priv = identity
    src_hash, log_path, cards_dir = _seed_source(tmp_path, card, priv, [{"id": 1}])

    # Tamper a non-content_hash field (ts_ns) on the source record. The content
    # hash still matches the anchor, so the record is *found*, but its signature
    # no longer verifies over the altered canonical bytes.
    line = json.loads(log_path.read_text().strip())
    line["ts_ns"] = line["ts_ns"] + 1
    log_path.write_text(json.dumps(line, separators=(",", ":"), sort_keys=True) + "\n", encoding="utf-8")

    verdict = verify_report_figures(
        canonical_bytes=_report("We migrated 1,234 users.\n", src_hash),
        log_path=log_path,
        cards_dir=cards_dir,
        operator_secret=_HMAC,
    )
    assert not verdict.ok
    assert any("does not verify" in f for f in verdict.failures)


def test_anchor_hmac_mismatch_fails(tmp_path: Path, identity: tuple[AgentCard, str]) -> None:
    card, priv = identity
    src_hash, log_path, cards_dir = _seed_source(tmp_path, card, priv, [{"id": 1}])

    resolver = LineageAnchorResolver(log_path=log_path, cards_dir=cards_dir, operator_secret=b"wrong-secret" * 4)
    res = resolver.resolve(FigureAnchor("artifact", src_hash))
    assert not res.ok
    assert "does not verify" in res.statement


def test_receipt_anchor_kind_is_a_plug_point(tmp_path: Path, identity: tuple[AgentCard, str]) -> None:
    card, priv = identity
    _src, log_path, cards_dir = _seed_source(tmp_path, card, priv, [{"id": 1}])

    resolver = LineageAnchorResolver(log_path=log_path, cards_dir=cards_dir, operator_secret=_HMAC)
    res = resolver.resolve(FigureAnchor("receipt", "rcpt-abc"))
    assert not res.ok
    assert "2887" in res.statement

    # A registered resolver plugs in without touching the evaluator.
    from bernstein.core.tasks.figures import AnchorResolution

    resolver.register("receipt", lambda ref: AnchorResolution(True, f"traces to receipt {ref}, recorded"))
    res2 = resolver.resolve(FigureAnchor("receipt", "rcpt-abc"))
    assert res2.ok
    assert "traces to receipt rcpt-abc" in res2.statement


# ---------------------------------------------------------------------------
# End to end: record a report bundle, then verify it through verify_artifact
# ---------------------------------------------------------------------------


def _seed_report_and_source(
    tmp_path: Path, card: AgentCard, priv: str, body: str, unanchored_ok: bool = True
) -> tuple[str, str]:
    """Record a source dataset + a report bundle anchoring to it into one .sdd.

    Returns (report_task_id, source_content_hash).
    """
    from bernstein.core.lineage.artifact_record import record_artifact

    sdd = tmp_path / ".sdd"
    store = LineageStore(sdd / "lineage")
    rec = LineageRecorder(store=store, operator_hmac_key=_HMAC)
    src = record_artifact(
        recorder=rec,
        sink_root=sdd / "artifacts",
        task_id="SRC-1",
        kind=ArtifactKind.DATASET,
        artifact=[{"users": 1234}],
        agent_id=card.agent_id,
        agent_card=card,
        private_key_pem=priv,
    )
    figs = (Figure("1,234", "users", "migrated users", FigureAnchor("artifact", src.content_hash)),)
    record_artifact(
        recorder=rec,
        sink_root=sdd / "artifacts",
        task_id="RPT-1",
        kind=ArtifactKind.REPORT,
        artifact=ReportBundle(body=body, figures=figs),
        agent_id=card.agent_id,
        agent_card=card,
        private_key_pem=priv,
    )
    _write_card(sdd / "agents", card)
    return "RPT-1", src.content_hash


def _verify(tmp_path: Path, task_id: str):
    from bernstein.core.lineage.artifact_record import verify_artifact

    sdd = tmp_path / ".sdd"
    return verify_artifact(
        task_id=task_id,
        sink_root=sdd / "artifacts",
        log_path=sdd / "lineage" / "log.jsonl",
        cards_dir=sdd / "agents",
        operator_secret=_HMAC,
    )


def test_verify_artifact_reports_grounded_report(tmp_path: Path, identity: tuple[AgentCard, str]) -> None:
    card, priv = identity
    task_id, _ = _seed_report_and_source(tmp_path, card, priv, "We migrated 1,234 users.\n")
    result = _verify(tmp_path, task_id)
    assert result.ok, result.failures
    assert result.figures is not None
    assert result.figures.has_figures
    assert result.figures.provenances[0].ok


def test_verify_artifact_fails_on_unanchored_number(tmp_path: Path, identity: tuple[AgentCard, str]) -> None:
    card, priv = identity
    # The body cites 9.9% but the sidecar only declares 1,234 users.
    task_id, _ = _seed_report_and_source(tmp_path, card, priv, "We migrated 1,234 users at 9.9% cost.\n")
    result = _verify(tmp_path, task_id)
    assert not result.ok
    assert result.figures is not None
    assert any("9.9%" in f for f in result.figures.failures)


def test_verify_artifact_fails_when_figure_edited_in_blob(tmp_path: Path, identity: tuple[AgentCard, str]) -> None:
    card, priv = identity
    task_id, _ = _seed_report_and_source(tmp_path, card, priv, "We migrated 1,234 users.\n")
    # Editing the stored figure value breaks the content hash (AC4) and the
    # edited figure value no longer matches the body's material number.
    blob = tmp_path / ".sdd" / "artifacts" / task_id / "artifact.bin"
    blob.write_bytes(blob.read_bytes().replace(b'"1,234"', b'"9,999"'))
    result = _verify(tmp_path, task_id)
    assert not result.ok
    # Content-hash re-derivation fails, and the body number is now unanchored.
    assert any("altered" in f for f in result.failures)
