"""Tests for :class:`bernstein.core.lineage.spine.LineageSpine`.

The spine is the single always-on Merkle-chained, HMAC-tagged lineage
store. Each entry is keyed by run id and chained by
``entry_hash = H(prev_hash, artifact_path, content_hash, actor,
step_id, model, timestamp)`` with an HMAC tag from the audit-chain key.

These tests pin the acceptance criteria of issue #2292:

* AC2 - ``verify`` recomputes the full hash chain and HMAC tags and
  fails on any single-byte mutation of any entry.
* AC3 - two byte-identical runs against fixtures produce byte-identical
  ``spine.jsonl`` (entry order and hashes included).
* AC5 - verifying an empty run returns a distinct ``no entries`` status
  rather than a trivial pass.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bernstein.core.lineage.spine import (
    LineageSpine,
    SpineStatus,
    compute_entry_hash,
)

_KEY = b"k" * 32


def _make_spine(tmp_path: Path, run_id: str = "run-1") -> LineageSpine:
    return LineageSpine(tmp_path / ".sdd" / "lineage", run_id=run_id, hmac_key=_KEY)


def test_record_appends_one_entry_per_write(tmp_path: Path) -> None:
    spine = _make_spine(tmp_path)
    spine.record(
        artifact_path="src/a.py",
        content=b"one",
        actor="agent:worker",
        step_id="s1",
        model="claude",
        timestamp=1,
    )
    spine.record(
        artifact_path="src/b.py",
        content=b"two",
        actor="agent:worker",
        step_id="s2",
        model="claude",
        timestamp=2,
    )
    lines = spine.spine_path.read_bytes().rstrip(b"\n").split(b"\n")
    assert len(lines) == 2


def test_head_file_tracks_latest_hash_and_hmac(tmp_path: Path) -> None:
    spine = _make_spine(tmp_path)
    h = spine.record(
        artifact_path="src/a.py",
        content=b"one",
        actor="agent:worker",
        step_id="s1",
        model="claude",
        timestamp=1,
    )
    head = json.loads(spine.head_path.read_text())
    assert head["head_hash"] == h
    assert head["hmac"]
    assert head["count"] == 1


def test_entry_hash_chains_previous(tmp_path: Path) -> None:
    spine = _make_spine(tmp_path)
    h1 = spine.record(
        artifact_path="src/a.py",
        content=b"one",
        actor="a",
        step_id="s1",
        model="m",
        timestamp=1,
    )
    entries = list(spine.iter_entries())
    assert entries[0].prev_hash == ""
    assert entries[0].entry_hash == h1
    # Recompute the second entry hash independently.
    h2 = spine.record(
        artifact_path="src/b.py",
        content=b"two",
        actor="a",
        step_id="s2",
        model="m",
        timestamp=2,
    )
    entries = list(spine.iter_entries())
    expected = compute_entry_hash(
        prev_hash=h1,
        artifact_path="src/b.py",
        content_hash=entries[1].content_hash,
        actor="a",
        step_id="s2",
        model="m",
        timestamp=2,
    )
    assert h2 == expected
    assert entries[1].prev_hash == h1


def test_verify_ok_on_intact_chain(tmp_path: Path) -> None:
    spine = _make_spine(tmp_path)
    for i in range(5):
        spine.record(
            artifact_path=f"src/{i}.py",
            content=f"c{i}".encode(),
            actor="a",
            step_id=f"s{i}",
            model="m",
            timestamp=i,
        )
    result = spine.verify()
    assert result.status is SpineStatus.OK
    assert result.ok
    assert result.count == 5
    assert result.errors == []


def test_verify_fails_on_single_byte_mutation(tmp_path: Path) -> None:
    """AC2: any single-byte mutation of any entry must be detected."""
    spine = _make_spine(tmp_path)
    for i in range(3):
        spine.record(
            artifact_path=f"src/{i}.py",
            content=f"c{i}".encode(),
            actor="a",
            step_id=f"s{i}",
            model="m",
            timestamp=i,
        )
    raw = spine.spine_path.read_bytes()
    # Flip a single byte inside the middle entry's payload.
    idx = raw.index(b"src/1.py")
    mutated = bytearray(raw)
    mutated[idx + 4] = mutated[idx + 4] ^ 0x01
    spine.spine_path.write_bytes(bytes(mutated))

    result = spine.verify()
    assert not result.ok
    assert result.status is SpineStatus.TAMPERED
    assert result.errors


def test_verify_fails_on_hmac_mutation(tmp_path: Path) -> None:
    spine = _make_spine(tmp_path)
    spine.record(
        artifact_path="src/a.py",
        content=b"one",
        actor="a",
        step_id="s1",
        model="m",
        timestamp=1,
    )
    raw = spine.spine_path.read_bytes()
    row = json.loads(raw)
    row["hmac"] = "0" * 64
    spine.spine_path.write_bytes((json.dumps(row) + "\n").encode())
    result = spine.verify()
    assert not result.ok
    assert result.status is SpineStatus.TAMPERED


def test_verify_wrong_key_fails(tmp_path: Path) -> None:
    spine = _make_spine(tmp_path)
    spine.record(
        artifact_path="src/a.py",
        content=b"one",
        actor="a",
        step_id="s1",
        model="m",
        timestamp=1,
    )
    other = LineageSpine(tmp_path / ".sdd" / "lineage", run_id="run-1", hmac_key=b"x" * 32)
    result = other.verify()
    assert not result.ok
    assert result.status is SpineStatus.TAMPERED


def test_empty_run_returns_no_entries_status(tmp_path: Path) -> None:
    """AC5: an empty run must not trivially pass."""
    spine = _make_spine(tmp_path, run_id="empty-run")
    result = spine.verify()
    assert result.status is SpineStatus.NO_ENTRIES
    assert not result.ok
    assert result.count == 0


def test_two_identical_runs_are_byte_identical(tmp_path: Path) -> None:
    """AC3: byte-identical runs produce byte-identical spine.jsonl."""
    fixtures = [
        ("src/a.py", b"alpha", "agent:1", "s1", "claude", 100),
        ("src/b.py", b"beta", "agent:1", "s2", "claude", 200),
        ("docs/c.md", b"gamma", "agent:2", "s3", "gemini", 300),
    ]

    def _run(root: Path) -> bytes:
        spine = LineageSpine(root / ".sdd" / "lineage", run_id="fix", hmac_key=_KEY)
        for path, content, actor, step, model, ts in fixtures:
            spine.record(
                artifact_path=path,
                content=content,
                actor=actor,
                step_id=step,
                model=model,
                timestamp=ts,
            )
        return spine.spine_path.read_bytes()

    a = _run(tmp_path / "run-a")
    b = _run(tmp_path / "run-b")
    assert a == b


def test_record_rejects_unsafe_paths(tmp_path: Path) -> None:
    spine = _make_spine(tmp_path)
    for bad in ("/etc/passwd", "../escape", "a/../../b"):
        with pytest.raises(ValueError):
            spine.record(
                artifact_path=bad,
                content=b"x",
                actor="a",
                step_id="s",
                model="m",
                timestamp=1,
            )


def test_content_hash_prefixed(tmp_path: Path) -> None:
    spine = _make_spine(tmp_path)
    spine.record(
        artifact_path="src/a.py",
        content=b"one",
        actor="a",
        step_id="s1",
        model="m",
        timestamp=1,
    )
    entry = next(iter(spine.iter_entries()))
    assert entry.content_hash.startswith("sha256:")
