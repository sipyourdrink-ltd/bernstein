"""Artifact recording + verification: the determinism/tamper heart (#2608).

These are the empirical criteria the design directive calls the heart of the
slice:

* a same-input double run produces a byte-identical ``content_hash`` *and* an
  identical signed lineage-entry hash - the artifact is a deterministic
  content-addressed record, not merely a logged blob;
* a one-byte input mutation changes the hash;
* ``verify_artifact`` fails on a post-hoc byte alteration of the blob, on a
  tampered log entry, and on a removed entry - so without lineage + signing
  there is no verifiable proof the agent produced the claimed result.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bernstein.core.lineage.artifact_record import (
    ArtifactReceipt,
    artifact_entry_path,
    load_receipt,
    record_artifact,
    verify_artifact,
)
from bernstein.core.lineage.identity import AgentCard, generate_keypair
from bernstein.core.lineage.signed_write import SignedLineageLog
from bernstein.core.lineage.store import LineageStore
from bernstein.core.tasks.artifacts import ArtifactKind

_HMAC_KEY = b"k" * 64


@pytest.fixture
def identity() -> tuple[AgentCard, str]:
    # A fixed keypair stands in for "the same operator identity" across runs.
    priv_pem, pub_pem = generate_keypair()
    card = AgentCard(agent_id="agent:report-worker", kid="key-artifact-001", public_key_pem=pub_pem)
    return card, priv_pem


def _recorder(root: Path) -> SignedLineageLog:
    return SignedLineageLog(store=LineageStore(root / "lineage"), operator_hmac_key=_HMAC_KEY)


def _write_card(cards_dir: Path, card: AgentCard) -> None:
    card_dir = cards_dir / card.agent_id
    card_dir.mkdir(parents=True, exist_ok=True)
    (card_dir / "card.json").write_text(
        json.dumps({"agent_id": card.agent_id, "kid": card.kid, "public_key_pem": card.public_key_pem}),
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# Determinism: same input -> byte-identical content_hash AND entry_hash
# ---------------------------------------------------------------------------


def test_same_input_double_run_is_byte_identical(tmp_path: Path, identity: tuple[AgentCard, str]) -> None:
    card, priv = identity
    rows = [{"id": 2, "name": "b"}, {"id": 1, "name": "a"}]

    r1 = record_artifact(
        recorder=_recorder(tmp_path / "run-a"),
        sink_root=tmp_path / "run-a" / "artifacts",
        task_id="T-1",
        kind=ArtifactKind.DATASET,
        artifact=rows,
        agent_id=card.agent_id,
        agent_card=card,
        private_key_pem=priv,
    )
    r2 = record_artifact(
        recorder=_recorder(tmp_path / "run-b"),
        sink_root=tmp_path / "run-b" / "artifacts",
        task_id="T-1",
        kind=ArtifactKind.DATASET,
        artifact=[dict(r) for r in rows],
        agent_id=card.agent_id,
        agent_card=card,
        private_key_pem=priv,
    )

    assert r1.content_hash == r2.content_hash
    # The completion receipt IS the signed lineage-entry hash: identical across
    # two independent runs with equal inputs (a deterministic projection).
    assert r1.entry_hash == r2.entry_hash


def test_one_byte_input_mutation_changes_the_hash(tmp_path: Path, identity: tuple[AgentCard, str]) -> None:
    card, priv = identity

    def _record(root: str, artifact: object) -> ArtifactReceipt:
        return record_artifact(
            recorder=_recorder(tmp_path / root),
            sink_root=tmp_path / root / "artifacts",
            task_id="T-2",
            kind=ArtifactKind.REPORT,
            artifact=artifact,
            agent_id=card.agent_id,
            agent_card=card,
            private_key_pem=priv,
        )

    base = _record("base", "the quick brown fox\n")
    mutated = _record("mut", "the quick brown frx\n")
    assert base.content_hash != mutated.content_hash
    assert base.entry_hash != mutated.entry_hash


# ---------------------------------------------------------------------------
# verify_artifact happy path + tamper detection
# ---------------------------------------------------------------------------


def _record_and_paths(tmp_path: Path, card: AgentCard, priv: str, artifact: object) -> tuple[Path, Path, Path]:
    sink_root = tmp_path / "artifacts"
    log_root = tmp_path / "lineage"
    cards_dir = tmp_path / "cards"
    record_artifact(
        recorder=SignedLineageLog(store=LineageStore(log_root), operator_hmac_key=_HMAC_KEY),
        sink_root=sink_root,
        task_id="T-9",
        kind=ArtifactKind.OPS_RESULT,
        artifact=artifact,
        agent_id=card.agent_id,
        agent_card=card,
        private_key_pem=priv,
    )
    _write_card(cards_dir, card)
    return sink_root, log_root / "log.jsonl", cards_dir


def test_verify_passes_for_untampered_artifact(tmp_path: Path, identity: tuple[AgentCard, str]) -> None:
    card, priv = identity
    sink_root, log_path, cards_dir = _record_and_paths(tmp_path, card, priv, {"status": "ok", "changed": 3})

    result = verify_artifact(
        task_id="T-9", sink_root=sink_root, log_path=log_path, cards_dir=cards_dir, operator_secret=_HMAC_KEY
    )
    assert result.ok, result.failures
    assert result.content_hash is not None
    assert result.entry_hash is not None


def test_verify_fails_on_byte_alteration_of_blob(tmp_path: Path, identity: tuple[AgentCard, str]) -> None:
    card, priv = identity
    sink_root, log_path, cards_dir = _record_and_paths(tmp_path, card, priv, {"status": "ok"})

    # Flip a byte in the stored canonical bytes after the fact.
    blob = sink_root / "T-9" / "artifact.bin"
    tampered = bytearray(blob.read_bytes())
    tampered[-2] ^= 0x20
    blob.write_bytes(bytes(tampered))

    result = verify_artifact(
        task_id="T-9", sink_root=sink_root, log_path=log_path, cards_dir=cards_dir, operator_secret=_HMAC_KEY
    )
    assert not result.ok
    assert any("altered" in f or "content_hash" in f for f in result.failures)


def test_verify_fails_on_tampered_log_entry(tmp_path: Path, identity: tuple[AgentCard, str]) -> None:
    card, priv = identity
    sink_root, log_path, cards_dir = _record_and_paths(tmp_path, card, priv, {"status": "ok"})

    # Rewrite the recorded content_hash in the log line to a plausible-looking
    # but forged value. The signature + HMAC no longer verify, and the entry
    # the receipt points at is gone.
    line = json.loads(log_path.read_text().strip())
    line["content_hash"] = "sha256:" + "0" * 64
    log_path.write_text(json.dumps(line, separators=(",", ":"), sort_keys=True) + "\n", encoding="utf-8")

    result = verify_artifact(
        task_id="T-9", sink_root=sink_root, log_path=log_path, cards_dir=cards_dir, operator_secret=_HMAC_KEY
    )
    assert not result.ok
    assert result.failures


def test_verify_fails_on_removed_entry(tmp_path: Path, identity: tuple[AgentCard, str]) -> None:
    card, priv = identity
    sink_root, log_path, cards_dir = _record_and_paths(tmp_path, card, priv, {"status": "ok"})

    # Remove the entry entirely (an empty log => the receipt's entry is gone).
    log_path.write_text("", encoding="utf-8")

    result = verify_artifact(
        task_id="T-9", sink_root=sink_root, log_path=log_path, cards_dir=cards_dir, operator_secret=_HMAC_KEY
    )
    assert not result.ok
    assert any("missing from the log" in f for f in result.failures)


def test_verify_fails_when_receipt_absent(tmp_path: Path) -> None:
    result = verify_artifact(
        task_id="nope",
        sink_root=tmp_path / "artifacts",
        log_path=tmp_path / "lineage" / "log.jsonl",
        cards_dir=tmp_path / "cards",
        operator_secret=_HMAC_KEY,
    )
    assert not result.ok
    assert any("no artifact receipt" in f for f in result.failures)


# ---------------------------------------------------------------------------
# Receipt + helpers
# ---------------------------------------------------------------------------


def test_receipt_round_trip() -> None:
    receipt = ArtifactReceipt(
        task_id="T",
        kind="report",
        content_hash="sha256:aa",
        entry_hash="sha256:bb",
        artefact_path=artifact_entry_path("T"),
        agent_id="agent:x",
        agent_card_kid="k1",
        ts_ns=0,
    )
    assert ArtifactReceipt.from_dict(receipt.to_dict()) == receipt


def test_load_receipt_returns_none_when_absent(tmp_path: Path) -> None:
    assert load_receipt(tmp_path, "missing") is None


def test_record_artifact_rejects_code_diff(tmp_path: Path, identity: tuple[AgentCard, str]) -> None:
    card, priv = identity
    with pytest.raises(ValueError, match="git-diff path"):
        record_artifact(
            recorder=_recorder(tmp_path),
            sink_root=tmp_path / "artifacts",
            task_id="T",
            kind=ArtifactKind.CODE_DIFF,
            artifact="@@ diff @@\n",
            agent_id=card.agent_id,
            agent_card=card,
            private_key_pem=priv,
        )


def test_artifact_entry_path_is_repo_relative() -> None:
    p = artifact_entry_path("T-42")
    assert p == ".sdd/artifacts/T-42/artifact.bin"
    assert not p.startswith("/")
