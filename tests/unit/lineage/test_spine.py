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
    _compute_hmac,
    compute_entry_hash,
    content_hash_of,
    verify_entry,
)
from bernstein.core.security.key_derivation import (
    DOMAIN_AUDIT,
    DOMAIN_LINEAGE,
    SCHEME_V1,
    SCHEME_V2,
    derive_store_key,
    domain_tag,
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
        domain_prefix=domain_tag(DOMAIN_LINEAGE, SCHEME_V2),
    )
    assert h2 == expected
    assert entries[1].prev_hash == h1


def test_record_entry_returns_the_full_entry(tmp_path: Path) -> None:
    """``record_entry`` hands back the appended entry, hmac included.

    Callers that need the HMAC tag of the entry they just wrote (for
    instance the A2A receipt issuer) can read it off the return value
    rather than walking the whole spine to find it again.
    """
    spine = _make_spine(tmp_path)
    entry = spine.record_entry(
        artifact_path="src/a.py",
        content=b"one",
        actor="a",
        step_id="s1",
        model="m",
        timestamp=1,
    )
    # The returned entry matches exactly what iter_entries reports, so the
    # hmac is the tag over this entry - not the head snapshot's hmac.
    (persisted,) = list(spine.iter_entries())
    assert entry.entry_hash == persisted.entry_hash
    assert entry.hmac == persisted.hmac
    assert entry.hmac
    # record() stays a thin wrapper returning just the entry hash.
    h2 = spine.record(
        artifact_path="src/b.py",
        content=b"two",
        actor="a",
        step_id="s2",
        model="m",
        timestamp=2,
    )
    second = list(spine.iter_entries())[1]
    assert h2 == second.entry_hash


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


# ---------------------------------------------------------------------------
# v2 scheme: derived keys, domain tags, backward compatibility
# ---------------------------------------------------------------------------


def _write_v1_row(
    spine: LineageSpine,
    *,
    artifact_path: str,
    content: bytes,
    actor: str,
    step_id: str,
    model: str,
    timestamp: int,
    prev_hash: str = "",
) -> str:
    """Append a legacy v1 entry directly to the JSONL (no domain tag, raw key)."""
    c_hash = content_hash_of(content)
    e_hash = compute_entry_hash(
        prev_hash=prev_hash,
        artifact_path=artifact_path,
        content_hash=c_hash,
        actor=actor,
        step_id=step_id,
        model=model,
        timestamp=timestamp,
    )
    body: dict[str, object] = {
        "v": SCHEME_V1,
        "prev_hash": prev_hash,
        "artifact_path": artifact_path,
        "content_hash": c_hash,
        "actor": actor,
        "step_id": step_id,
        "model": model,
        "timestamp": timestamp,
        "entry_hash": e_hash,
    }
    tag = _compute_hmac(_KEY, body)
    row = body | {"hmac": tag}
    spine.run_dir.mkdir(parents=True, exist_ok=True)
    with spine.spine_path.open("ab") as fh:
        fh.write((json.dumps(row, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n").encode("utf-8"))
    return e_hash


def test_new_entries_are_v2_with_domain_tag(tmp_path: Path) -> None:
    """New entries are written as v2 with HKDF-derived key and domain-tagged hash."""
    import hmac as _hmac

    spine = _make_spine(tmp_path)
    entry = spine.record_entry(
        artifact_path="src/a.py",
        content=b"one",
        actor="a",
        step_id="s1",
        model="m",
        timestamp=1,
    )
    assert entry.v == SCHEME_V2

    # The entry hash must include the domain tag in the preimage.
    expected_hash = compute_entry_hash(
        prev_hash="",
        artifact_path="src/a.py",
        content_hash=entry.content_hash,
        actor="a",
        step_id="s1",
        model="m",
        timestamp=1,
        domain_prefix=domain_tag(DOMAIN_LINEAGE, SCHEME_V2),
    )
    assert entry.entry_hash == expected_hash

    # The HMAC must be computed with the lineage-derived key, not the raw key.
    derived = derive_store_key(_KEY, DOMAIN_LINEAGE)
    assert _hmac.compare_digest(entry.hmac, _compute_hmac(derived, entry.body()))
    assert not _hmac.compare_digest(entry.hmac, _compute_hmac(_KEY, entry.body()))


def test_verify_v1_entries_backward_compat(tmp_path: Path) -> None:
    """Existing v1 entries still verify (backward compatibility)."""
    spine = _make_spine(tmp_path)
    _write_v1_row(
        spine,
        artifact_path="src/legacy.py",
        content=b"legacy",
        actor="legacy-agent",
        step_id="s0",
        model="old-model",
        timestamp=0,
    )
    result = spine.verify()
    assert result.status is SpineStatus.OK
    assert result.ok
    assert result.count == 1
    assert result.errors == []


def test_mixed_v1_v2_chain_verifies(tmp_path: Path) -> None:
    """A chain with v1 entries followed by v2 entries verifies correctly."""
    spine = _make_spine(tmp_path)

    # Write a v1 entry manually.
    h1 = _write_v1_row(
        spine,
        artifact_path="src/v1.py",
        content=b"v1-content",
        actor="a",
        step_id="s1",
        model="m",
        timestamp=1,
    )

    # Append a v2 entry through the normal API (chains from the v1 entry).
    spine.record(
        artifact_path="src/v2.py",
        content=b"v2-content",
        actor="a",
        step_id="s2",
        model="m",
        timestamp=2,
    )

    entries = list(spine.iter_entries())
    assert len(entries) == 2
    assert entries[0].v == SCHEME_V1
    assert entries[1].v == SCHEME_V2
    assert entries[1].prev_hash == h1

    result = spine.verify()
    assert result.status is SpineStatus.OK
    assert result.ok
    assert result.count == 2
    assert result.errors == []


def test_cross_store_isolation_audit_key_fails(tmp_path: Path) -> None:
    """A lineage tag fails if the audit-derived key is used (cross-store isolation)."""
    import hmac as _hmac

    spine = _make_spine(tmp_path)
    entry = spine.record_entry(
        artifact_path="src/a.py",
        content=b"one",
        actor="a",
        step_id="s1",
        model="m",
        timestamp=1,
    )

    # The lineage HMAC tag must NOT match the audit-derived key's HMAC.
    audit_key = derive_store_key(_KEY, DOMAIN_AUDIT)
    assert not _hmac.compare_digest(
        entry.hmac,
        _compute_hmac(audit_key, entry.body()),
    )

    # The entry hash must NOT match without the domain prefix (v1-style).
    v1_hash = compute_entry_hash(
        prev_hash=entry.prev_hash,
        artifact_path=entry.artifact_path,
        content_hash=entry.content_hash,
        actor=entry.actor,
        step_id=entry.step_id,
        model=entry.model,
        timestamp=entry.timestamp,
    )
    assert entry.entry_hash != v1_hash


def test_verify_entry_handles_v1_and_v2(tmp_path: Path) -> None:
    """verify_entry auto-detects the entry version and uses the correct key."""
    spine = _make_spine(tmp_path)

    _write_v1_row(
        spine,
        artifact_path="src/v1.py",
        content=b"v1",
        actor="a",
        step_id="s1",
        model="m",
        timestamp=1,
    )
    spine.record(
        artifact_path="src/v2.py",
        content=b"v2",
        actor="a",
        step_id="s2",
        model="m",
        timestamp=2,
    )

    entries = list(spine.iter_entries())
    assert verify_entry(entries[0], _KEY)  # v1
    assert verify_entry(entries[1], _KEY)  # v2

    # A wrong master key fails for both versions.
    assert not verify_entry(entries[0], b"x" * 32)
    assert not verify_entry(entries[1], b"x" * 32)
