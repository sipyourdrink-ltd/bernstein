"""Served-from spine entries are Merkle-chained and tamper-evident (AC2).

A run that served steps from cache replays to the identical spine head;
suppressing or mutating any single served_from entry causes spine verification
to fail, so a hidden or forged cache hit is falsification-evident.
"""

from __future__ import annotations

from pathlib import Path

from bernstein.core.lineage.spine import LineageSpine, SpineStatus
from bernstein.core.persistence.cache_served_from import (
    record_served_from,
    served_from_artifact_path,
)

_HMAC = b"k" * 32


def _spine(tmp_path: Path, run_id: str = "run-1") -> LineageSpine:
    return LineageSpine(tmp_path / "lineage", run_id=run_id, hmac_key=_HMAC)


def _record_two_hits(spine: LineageSpine) -> None:
    record_served_from(
        spine,
        cache_key="key-a",
        output_hash="sha256:out-a",
        policy_hash="sha256:policy",
        recipe_hash="sha256:recipe",
        actor="cache_policy",
        step_id="step-1",
        model="claude-opus-4-8",
        timestamp=1000,
    )
    record_served_from(
        spine,
        cache_key="key-b",
        output_hash="sha256:out-b",
        policy_hash="sha256:policy",
        recipe_hash="sha256:recipe",
        actor="cache_policy",
        step_id="step-2",
        model="claude-opus-4-8",
        timestamp=1001,
    )


def test_served_from_hits_verify(tmp_path: Path) -> None:
    spine = _spine(tmp_path)
    _record_two_hits(spine)
    result = spine.verify()
    assert result.status is SpineStatus.OK
    assert result.count == 2


def test_two_runs_replay_to_identical_head(tmp_path: Path) -> None:
    # AC2: byte-identical served_from records replay to the identical head.
    a = _spine(tmp_path / "one")
    b = _spine(tmp_path / "two")
    _record_two_hits(a)
    _record_two_hits(b)
    assert a.head_hash() == b.head_hash()


def test_mutated_served_from_entry_fails_verification(tmp_path: Path) -> None:
    import json

    spine = _spine(tmp_path)
    _record_two_hits(spine)
    # Tamper with the first served_from row: forge the recorded content hash so
    # the served output no longer matches what the entry claims was served.
    lines = spine.spine_path.read_text(encoding="utf-8").splitlines()
    row = json.loads(lines[0])
    row["content_hash"] = "sha256:" + "f" * 64
    lines[0] = json.dumps(row, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    spine.spine_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    assert spine.verify().status is SpineStatus.TAMPERED


def test_suppressed_served_from_entry_fails_verification(tmp_path: Path) -> None:
    spine = _spine(tmp_path)
    _record_two_hits(spine)
    lines = spine.spine_path.read_text(encoding="utf-8").splitlines()
    # Drop the first hit; the second's prev_hash no longer chains to genesis.
    spine.spine_path.write_text(lines[1] + "\n", encoding="utf-8")
    assert spine.verify().status is SpineStatus.TAMPERED


def test_artifact_path_is_repo_relative() -> None:
    path = served_from_artifact_path("key-a")
    assert path == ".sdd/cache/served_from/key-a"
    assert not path.startswith("/")
