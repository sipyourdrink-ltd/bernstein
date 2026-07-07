"""Verification evidence bundle tests (issue #2362).

Each test maps to an acceptance criterion from the issue:

* AC1 -- a completed task with declared producers always has a bundle whose
  integrity verifies offline.
* AC2 -- tampering with any evidence file breaks verification with the file
  named.
* AC3 -- the PR summary block links the bundle (covered in
  ``test_pr_gen_evidence.py`` / ``test_evidence_projection.py``).
* AC4 -- deterministic producers regenerate byte-identical bundle hashes on
  replay.
* AC5 -- an advisory producer failure attaches a failure record without
  blocking the gate.
"""

from __future__ import annotations

from pathlib import Path

from bernstein.core.evidence.bundle import (
    EvidenceProducer,
    EvidenceStore,
    ProducerOutcome,
    build_evidence_bundle,
    load_or_create_evidence_identity,
    parse_producers,
    read_evidence_bundle,
    run_evidence_gate,
    run_producers,
    verify_evidence_bundle,
)

_KEY = b"0" * 32


def _identity(tmp_path: Path) -> tuple[str, str]:
    return load_or_create_evidence_identity(tmp_path / ".sdd" / "identity")


def _outcome(name: str, kind: str, *, required: bool, exit_code: int, output: bytes) -> ProducerOutcome:
    return ProducerOutcome(
        producer=EvidenceProducer(name=name, kind=kind, command=("run", name), required=required),
        exit_code=exit_code,
        output=output,
    )


def _pass_outcomes() -> tuple[ProducerOutcome, ...]:
    return (
        _outcome("tests", "test", required=True, exit_code=0, output=b"12 passed\n"),
        _outcome("coverage", "coverage", required=True, exit_code=0, output=b"line-rate 0.94\n"),
        _outcome("lint", "lint", required=False, exit_code=0, output=b"0 findings\n"),
    )


def _build(
    tmp_path: Path,
    outcomes: tuple[ProducerOutcome, ...],
    *,
    task_id: str = "task-1",
    timestamp: int = 1000,
) -> object:
    priv, pub = _identity(tmp_path)
    return build_evidence_bundle(
        workdir=tmp_path,
        lineage_root=tmp_path / ".sdd" / "lineage",
        hmac_key=_KEY,
        private_key_pem=priv,
        public_key_pem=pub,
        task_id=task_id,
        outcomes=outcomes,
        timestamp=timestamp,
    )


# ---------------------------------------------------------------------------
# Producer contract
# ---------------------------------------------------------------------------


def test_parse_producers_from_task_spec() -> None:
    raw = [
        {"name": "tests", "kind": "test", "command": ["pytest", "-q"], "required": True},
        {"name": "shot", "kind": "screenshot", "command": ["shot.sh"], "required": False},
    ]
    producers = parse_producers(raw)
    assert len(producers) == 2
    assert producers[0].name == "tests"
    assert producers[0].required is True
    assert producers[0].command == ("pytest", "-q")
    assert producers[1].kind == "screenshot"
    assert producers[1].is_media() is True
    assert producers[0].is_media() is False


def test_parse_producers_rejects_unknown_kind() -> None:
    import pytest

    with pytest.raises(ValueError, match="kind"):
        parse_producers([{"name": "x", "kind": "nonsense", "command": ["y"]}])


def test_run_producers_captures_exit_and_output() -> None:
    producers = (
        EvidenceProducer(name="tests", kind="test", command=("run",), required=True),
        EvidenceProducer(name="lint", kind="lint", command=("lint",), required=False),
    )

    def runner(p: EvidenceProducer) -> tuple[int, bytes]:
        return (0, b"ok") if p.name == "tests" else (3, b"warn")

    outcomes = run_producers(producers, runner=runner)
    assert outcomes[0].exit_code == 0
    assert outcomes[0].passed is True
    assert outcomes[1].exit_code == 3
    assert outcomes[1].passed is False
    assert outcomes[1].output == b"warn"


# ---------------------------------------------------------------------------
# Content-addressed store: caps + gc
# ---------------------------------------------------------------------------


def test_store_is_content_addressed(tmp_path: Path) -> None:
    store = EvidenceStore(tmp_path / ".sdd" / "evidence")
    a = store.put(b"same bytes")
    b = store.put(b"same bytes")
    assert a.content_hash == b.content_hash
    assert store.get(a.content_hash) == b"same bytes"
    assert store.has(a.content_hash)


def test_store_applies_size_cap(tmp_path: Path) -> None:
    store = EvidenceStore(tmp_path / ".sdd" / "evidence", max_blob_bytes=10)
    stored = store.put(b"x" * 100)
    assert stored.truncated is True
    assert stored.size == 10
    assert stored.original_size == 100
    assert store.get(stored.content_hash) == b"x" * 10


def test_store_gc_removes_orphan_blobs(tmp_path: Path) -> None:
    store = EvidenceStore(tmp_path / ".sdd" / "evidence")
    keep = store.put(b"keep me")
    drop1 = store.put(b"drop one")
    drop2 = store.put(b"drop two")
    removed = store.gc({keep.content_hash})
    assert removed == 2
    assert store.get(keep.content_hash) == b"keep me"
    assert store.get(drop1.content_hash) is None
    assert store.get(drop2.content_hash) is None


# ---------------------------------------------------------------------------
# AC1 -- a completed task with declared producers has a bundle that verifies
# ---------------------------------------------------------------------------


def test_completed_task_bundle_verifies_offline(tmp_path: Path) -> None:
    bundle = _build(tmp_path, _pass_outcomes())
    assert bundle.gate_passed is True
    assert bundle.journal_entry_hash
    assert bundle.signature
    assert bundle.signer_public_key_pem
    assert len(bundle.items) == 3

    result = verify_evidence_bundle(
        workdir=tmp_path,
        lineage_root=tmp_path / ".sdd" / "lineage",
        hmac_key=_KEY,
        task_id="task-1",
    )
    assert result.ok, result.reason
    assert result.bundle is not None
    assert result.bundle.task_id == "task-1"


def test_bundle_persists_and_reloads(tmp_path: Path) -> None:
    bundle = _build(tmp_path, _pass_outcomes())
    reloaded = read_evidence_bundle(tmp_path, "task-1")
    assert reloaded is not None
    assert reloaded.to_dict() == bundle.to_dict()


def test_verify_no_bundle(tmp_path: Path) -> None:
    (tmp_path / ".sdd").mkdir()
    result = verify_evidence_bundle(
        workdir=tmp_path,
        lineage_root=tmp_path / ".sdd" / "lineage",
        hmac_key=_KEY,
        task_id="missing",
    )
    assert not result.ok
    assert result.bundle is None


# ---------------------------------------------------------------------------
# AC2 -- tampering with any evidence file breaks verify with the file named
# ---------------------------------------------------------------------------


def test_tampered_blob_breaks_verify_and_names_file(tmp_path: Path) -> None:
    bundle = _build(tmp_path, _pass_outcomes())
    # The stored blob for the "tests" producer output.
    tests_item = next(i for i in bundle.items if i.name == "tests")
    store = EvidenceStore(tmp_path / ".sdd" / "evidence")
    blob = store.blob_path(tests_item.content_hash)
    blob.write_bytes(b"999 passed\n")  # forge the runner output

    result = verify_evidence_bundle(
        workdir=tmp_path,
        lineage_root=tmp_path / ".sdd" / "lineage",
        hmac_key=_KEY,
        task_id="task-1",
    )
    assert not result.ok
    assert "tests" in result.tampered_items
    assert "tests" in result.reason


def test_tampered_bundle_signature_breaks_verify(tmp_path: Path) -> None:
    from bernstein.core.evidence.bundle import bundle_path

    _build(tmp_path, _pass_outcomes())
    path = bundle_path(tmp_path, "task-1")
    raw = path.read_text(encoding="utf-8").replace('"pass"', '"fail"')
    path.write_text(raw, encoding="utf-8")

    result = verify_evidence_bundle(
        workdir=tmp_path,
        lineage_root=tmp_path / ".sdd" / "lineage",
        hmac_key=_KEY,
        task_id="task-1",
    )
    assert not result.ok


def test_tampered_spine_breaks_verify(tmp_path: Path) -> None:
    _build(tmp_path, _pass_outcomes())
    spine_log = next((tmp_path / ".sdd" / "lineage").rglob("spine.jsonl"))
    raw = spine_log.read_bytes().replace(
        b'"actor":"bernstein.evidence_bundle"',
        b'"actor":"bernstein.evidence_tampered"',
    )
    assert b"evidence_tampered" in raw
    spine_log.write_bytes(raw)

    result = verify_evidence_bundle(
        workdir=tmp_path,
        lineage_root=tmp_path / ".sdd" / "lineage",
        hmac_key=_KEY,
        task_id="task-1",
    )
    assert not result.ok


# ---------------------------------------------------------------------------
# AC4 -- deterministic producers regenerate byte-identical bundle hashes
# ---------------------------------------------------------------------------


def test_deterministic_producers_byte_identical_bundle(tmp_path: Path) -> None:
    root_a = tmp_path / "a"
    root_b = tmp_path / "b"
    root_a.mkdir()
    root_b.mkdir()

    # Same signing identity for both so the anchor + signature are comparable.
    priv, pub = load_or_create_evidence_identity(tmp_path / "identity")

    def build(root: Path) -> object:
        return build_evidence_bundle(
            workdir=root,
            lineage_root=root / ".sdd" / "lineage",
            hmac_key=_KEY,
            private_key_pem=priv,
            public_key_pem=pub,
            task_id="task-det",
            outcomes=_pass_outcomes(),
            timestamp=4242,
        )

    a = build(root_a)
    b = build(root_b)
    assert a.bundle_hash() == b.bundle_hash()
    assert a.to_canonical_bytes() == b.to_canonical_bytes()
    # Ed25519 is deterministic and the spine starts from genesis in both roots,
    # so the signature and the spine anchor are byte-identical too.
    assert a.signature == b.signature
    assert a.journal_entry_hash == b.journal_entry_hash


# ---------------------------------------------------------------------------
# AC5 -- advisory producer failure attaches a failure record without blocking
# ---------------------------------------------------------------------------


def test_advisory_failure_attaches_without_blocking_gate(tmp_path: Path) -> None:
    outcomes = (
        _outcome("tests", "test", required=True, exit_code=0, output=b"ok\n"),
        _outcome("lint", "lint", required=False, exit_code=7, output=b"3 findings\n"),
    )
    bundle = _build(tmp_path, outcomes)
    assert bundle.gate_passed is True  # advisory failure does not block
    lint_item = next(i for i in bundle.items if i.name == "lint")
    assert lint_item.status == "fail"
    assert lint_item.required is False
    assert lint_item.exit_code == 7
    # The failure record is attached and content-addressed.
    assert lint_item.content_hash


def test_required_failure_blocks_gate(tmp_path: Path) -> None:
    outcomes = (
        _outcome("tests", "test", required=True, exit_code=1, output=b"1 failed\n"),
        _outcome("lint", "lint", required=False, exit_code=0, output=b"ok\n"),
    )
    bundle = _build(tmp_path, outcomes)
    assert bundle.gate_passed is False
    tests_item = next(i for i in bundle.items if i.name == "tests")
    assert tests_item.status == "fail"
    assert tests_item.required is True


# ---------------------------------------------------------------------------
# run_evidence_gate -- the gate-time entrypoint (produce -> seal -> return)
# ---------------------------------------------------------------------------


def test_run_evidence_gate_produces_verifiable_bundle(tmp_path: Path) -> None:
    producers = (
        EvidenceProducer(name="tests", kind="test", command=("run",), required=True),
        EvidenceProducer(name="lint", kind="lint", command=("lint",), required=False),
    )

    def runner(p: EvidenceProducer) -> tuple[int, bytes]:
        return (0, b"ok\n") if p.name == "tests" else (5, b"advisory boom\n")

    bundle, gate_passed = run_evidence_gate(
        workdir=tmp_path,
        task_id="gate-task",
        producers=producers,
        runner=runner,
        timestamp=99,
        hmac_key=_KEY,
    )
    assert gate_passed is True  # required passed; advisory failure attached
    assert bundle.gate_passed is True

    result = verify_evidence_bundle(
        workdir=tmp_path,
        lineage_root=tmp_path / ".sdd" / "lineage",
        hmac_key=_KEY,
        task_id="gate-task",
    )
    assert result.ok, result.reason

    # The bundle is mirrored into the HMAC-chained audit log.
    from bernstein.core.security.audit_chain import EVENT_EVIDENCE_BUNDLE, AuditChainStore

    chain = AuditChainStore(tmp_path / ".sdd" / "audit", key=_KEY)
    rows = chain.query(event_type=EVENT_EVIDENCE_BUNDLE)
    assert len(rows) == 1
    assert rows[0].details["task_id"] == "gate-task"


# ---------------------------------------------------------------------------
# Media evidence flows through the content-credentials (C2PA) support
# ---------------------------------------------------------------------------


def test_media_producer_gets_content_credential(tmp_path: Path) -> None:
    outcomes = (
        _outcome("tests", "test", required=True, exit_code=0, output=b"ok\n"),
        _outcome("shot", "screenshot", required=False, exit_code=0, output=b"\x89PNG\r\n\x1a\nfake"),
    )
    bundle = _build(tmp_path, outcomes)
    shot = next(i for i in bundle.items if i.name == "shot")
    assert shot.content_credential_hash  # a C2PA manifest was projected + stored
    # The bundle still verifies with the media credential in place.
    result = verify_evidence_bundle(
        workdir=tmp_path,
        lineage_root=tmp_path / ".sdd" / "lineage",
        hmac_key=_KEY,
        task_id="task-1",
    )
    assert result.ok, result.reason


def test_tampered_media_blob_breaks_verify(tmp_path: Path) -> None:
    outcomes = (_outcome("shot", "screenshot", required=False, exit_code=0, output=b"\x89PNG\r\n\x1a\nfake"),)
    bundle = _build(tmp_path, outcomes)
    shot = next(i for i in bundle.items if i.name == "shot")
    store = EvidenceStore(tmp_path / ".sdd" / "evidence")
    store.blob_path(shot.content_hash).write_bytes(b"\x89PNG\r\n\x1a\nFORGED")
    result = verify_evidence_bundle(
        workdir=tmp_path,
        lineage_root=tmp_path / ".sdd" / "lineage",
        hmac_key=_KEY,
        task_id="task-1",
    )
    assert not result.ok
    assert "shot" in result.tampered_items


# ---------------------------------------------------------------------------
# The default subprocess runner (production gate-time execution path)
# ---------------------------------------------------------------------------


def test_default_subprocess_runner_captures_real_exit_and_output(tmp_path: Path) -> None:
    import sys

    producers = (
        EvidenceProducer(
            name="tests",
            kind="test",
            command=(sys.executable, "-c", "print('hello evidence')"),
            required=True,
        ),
        EvidenceProducer(
            name="lint",
            kind="lint",
            command=(sys.executable, "-c", "import sys; sys.exit(2)"),
            required=False,
        ),
    )
    # No runner injected: the real subprocess runner rooted at ``tmp_path`` runs.
    bundle, gate_passed = run_evidence_gate(
        workdir=tmp_path,
        task_id="subproc-task",
        producers=producers,
        timestamp=7,
        hmac_key=_KEY,
    )
    assert gate_passed is True  # required producer exited 0; advisory exit 2 attaches
    tests_item = next(i for i in bundle.items if i.name == "tests")
    store = EvidenceStore(tmp_path / ".sdd" / "evidence")
    assert b"hello evidence" in (store.get(tests_item.content_hash) or b"")
    lint_item = next(i for i in bundle.items if i.name == "lint")
    assert lint_item.exit_code == 2
    assert lint_item.status == "fail"
