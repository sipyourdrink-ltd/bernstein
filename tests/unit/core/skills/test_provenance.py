"""Unit tests for skill usage provenance (issue #2301).

Covers the acceptance criteria:

1. Installing a skill writes a lineage receipt anchored to the spine.
2. Each run using a skill links skill_hash to the run journal head.
3. ``skill provenance`` returns only runs whose journal heads verify.
4. A usage-count claim is recomputable from journal heads, not stored as
   a mutable counter.
5. ``skill verify`` detects a manifest_hash mismatch against the
   installed content.
"""

from __future__ import annotations

import json
from pathlib import Path

from bernstein.core.lineage.spine import LineageSpine
from bernstein.core.skills.provenance import (
    InstallReceipt,
    UsageLink,
    provenance_graph,
    read_install_receipt,
    record_usage,
    usage_index_path,
    verify_install,
    write_install_receipt,
)

_KEY = b"0" * 32
_SKILL_HASH = "sha256:" + "a" * 64
_MANIFEST_HASH = "b" * 64


def _lineage_root(workdir: Path) -> Path:
    return workdir / ".sdd" / "lineage"


def _make_run(workdir: Path, run_id: str, *, tamper: bool = False) -> str:
    """Create a run spine with one artifact write; return its head hash."""
    root = _lineage_root(workdir)
    spine = LineageSpine(root, run_id=run_id, hmac_key=_KEY)
    spine.record(
        artifact_path=f"out/{run_id}.txt",
        content=b"payload",
        actor="worker",
        step_id="step-1",
        model="test-model",
        timestamp=1,
    )
    head = spine.head_hash()
    if tamper:
        spine_path = root / run_id / "spine.jsonl"
        raw = spine_path.read_bytes()
        spine_path.write_bytes(raw.replace(b"payload", b"PAYLOAD") if b"payload" in raw else raw[:-2] + b"X\n")
    return head


# ---------------------------------------------------------------------------
# AC1 - install writes a lineage receipt anchored to the spine
# ---------------------------------------------------------------------------


def test_write_install_receipt_anchors_to_spine(tmp_path: Path) -> None:
    receipt = InstallReceipt(
        skill_hash=_SKILL_HASH,
        manifest_hash=_MANIFEST_HASH,
        install_id="install-1",
        timestamp=42,
    )
    anchor = write_install_receipt(
        workdir=tmp_path,
        lineage_root=_lineage_root(tmp_path),
        hmac_key=_KEY,
        receipt=receipt,
    )
    assert anchor.startswith("sha256:")

    # The receipt file exists on disk and round-trips.
    loaded = read_install_receipt(tmp_path, _SKILL_HASH)
    assert loaded == receipt

    # The install-provenance spine verifies and its head equals the anchor.
    spine = LineageSpine(_lineage_root(tmp_path), run_id="skills", hmac_key=_KEY)
    assert spine.verify().ok
    assert spine.head_hash() == anchor


def test_write_install_receipt_is_deterministic(tmp_path: Path) -> None:
    receipt = InstallReceipt(
        skill_hash=_SKILL_HASH,
        manifest_hash=_MANIFEST_HASH,
        install_id="install-1",
        timestamp=42,
    )
    a = write_install_receipt(
        workdir=tmp_path / "a",
        lineage_root=_lineage_root(tmp_path / "a"),
        hmac_key=_KEY,
        receipt=receipt,
    )
    b = write_install_receipt(
        workdir=tmp_path / "b",
        lineage_root=_lineage_root(tmp_path / "b"),
        hmac_key=_KEY,
        receipt=receipt,
    )
    assert a == b


# ---------------------------------------------------------------------------
# AC2 - each run using a skill links skill_hash to the run journal head
# ---------------------------------------------------------------------------


def test_record_usage_links_skill_hash_to_journal_head(tmp_path: Path) -> None:
    head = _make_run(tmp_path, "run-1")
    link = record_usage(
        workdir=tmp_path,
        skill_hash=_SKILL_HASH,
        run_id="run-1",
        journal_head=head,
        timestamp=7,
    )
    assert isinstance(link, UsageLink)
    assert link.skill_hash == _SKILL_HASH
    assert link.run_id == "run-1"
    assert link.journal_head == head

    index = usage_index_path(tmp_path, _SKILL_HASH)
    rows = [json.loads(line) for line in index.read_text().splitlines() if line]
    assert rows[0]["run_id"] == "run-1"
    assert rows[0]["journal_head"] == head


# ---------------------------------------------------------------------------
# AC3 + AC4 - provenance returns only verified runs; count is recomputed
# ---------------------------------------------------------------------------


def test_provenance_graph_returns_only_verified_runs(tmp_path: Path) -> None:
    good = _make_run(tmp_path, "run-good")
    bad_head = _make_run(tmp_path, "run-bad")
    record_usage(workdir=tmp_path, skill_hash=_SKILL_HASH, run_id="run-good", journal_head=good, timestamp=1)
    record_usage(workdir=tmp_path, skill_hash=_SKILL_HASH, run_id="run-bad", journal_head=bad_head, timestamp=2)

    # Tamper with run-bad after the link was recorded.
    _make_run(tmp_path, "run-bad", tamper=True)

    graph = provenance_graph(
        workdir=tmp_path,
        lineage_root=_lineage_root(tmp_path),
        hmac_key=_KEY,
        skill_hash=_SKILL_HASH,
    )
    verified_ids = {r.run_id for r in graph.verified_runs}
    assert verified_ids == {"run-good"}
    assert graph.verified_run_count == 1
    # The tampered run is surfaced separately, not counted.
    assert "run-bad" in {r.run_id for r in graph.unverified_runs}


def test_provenance_head_mismatch_is_unverified(tmp_path: Path) -> None:
    """A link whose recorded head no longer matches the spine head fails."""
    head = _make_run(tmp_path, "run-1")
    record_usage(workdir=tmp_path, skill_hash=_SKILL_HASH, run_id="run-1", journal_head=head, timestamp=1)
    # Append another artifact: the spine head advances past the linked head.
    LineageSpine(_lineage_root(tmp_path), run_id="run-1", hmac_key=_KEY).record(
        artifact_path="out/extra.txt",
        content=b"more",
        actor="worker",
        step_id="s2",
        model="m",
        timestamp=2,
    )
    graph = provenance_graph(
        workdir=tmp_path,
        lineage_root=_lineage_root(tmp_path),
        hmac_key=_KEY,
        skill_hash=_SKILL_HASH,
    )
    assert graph.verified_run_count == 0
    assert {r.run_id for r in graph.unverified_runs} == {"run-1"}


def test_provenance_count_recomputed_not_stored(tmp_path: Path) -> None:
    """AC4: usage count is a function of verified heads, not a counter.

    Recording the same run twice must not inflate the verified count -
    the count derives from the distinct set of verified journal heads.
    """
    head = _make_run(tmp_path, "run-1")
    record_usage(workdir=tmp_path, skill_hash=_SKILL_HASH, run_id="run-1", journal_head=head, timestamp=1)
    record_usage(workdir=tmp_path, skill_hash=_SKILL_HASH, run_id="run-1", journal_head=head, timestamp=2)
    graph = provenance_graph(
        workdir=tmp_path,
        lineage_root=_lineage_root(tmp_path),
        hmac_key=_KEY,
        skill_hash=_SKILL_HASH,
    )
    assert graph.verified_run_count == 1


# ---------------------------------------------------------------------------
# AC5 - skill verify detects manifest_hash mismatch against installed content
# ---------------------------------------------------------------------------


def test_verify_install_ok_when_content_matches(tmp_path: Path) -> None:
    receipt = InstallReceipt(
        skill_hash=_SKILL_HASH,
        manifest_hash=_MANIFEST_HASH,
        install_id="install-1",
        timestamp=42,
    )
    write_install_receipt(
        workdir=tmp_path,
        lineage_root=_lineage_root(tmp_path),
        hmac_key=_KEY,
        receipt=receipt,
    )
    result = verify_install(
        workdir=tmp_path,
        lineage_root=_lineage_root(tmp_path),
        hmac_key=_KEY,
        skill_hash=_SKILL_HASH,
        installed_manifest_hash=_MANIFEST_HASH,
    )
    assert result.ok
    assert result.receipt == receipt


def test_verify_install_detects_manifest_mismatch(tmp_path: Path) -> None:
    receipt = InstallReceipt(
        skill_hash=_SKILL_HASH,
        manifest_hash=_MANIFEST_HASH,
        install_id="install-1",
        timestamp=42,
    )
    write_install_receipt(
        workdir=tmp_path,
        lineage_root=_lineage_root(tmp_path),
        hmac_key=_KEY,
        receipt=receipt,
    )
    result = verify_install(
        workdir=tmp_path,
        lineage_root=_lineage_root(tmp_path),
        hmac_key=_KEY,
        skill_hash=_SKILL_HASH,
        installed_manifest_hash="c" * 64,  # drifted
    )
    assert not result.ok
    assert "manifest" in result.reason.lower()


def test_verify_install_missing_receipt(tmp_path: Path) -> None:
    result = verify_install(
        workdir=tmp_path,
        lineage_root=_lineage_root(tmp_path),
        hmac_key=_KEY,
        skill_hash=_SKILL_HASH,
        installed_manifest_hash=_MANIFEST_HASH,
    )
    assert not result.ok
    assert "receipt" in result.reason.lower()
