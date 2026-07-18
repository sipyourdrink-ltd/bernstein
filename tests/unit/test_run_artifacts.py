"""Agent-posted, journal-anchored task artifacts (#2553).

These tests exercise the substrate coupling the ticket demands: every posted
artifact is content-addressed, spine-sealed, and journal-chained, so a single
flipped byte -- in the stored blob or in the journal row -- fails verification
naming the artifact, and the progress vector cannot be inflated by posting.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from bernstein.core.evidence.bundle import EvidenceStore
from bernstein.core.evidence.run_artifacts import (
    ArtifactPayload,
    ArtifactTooLargeError,
    ArtifactValidationError,
    latest_versions,
    live_artifact_content_hashes,
    post_run_artifact,
    read_artifact_rows,
    verify_all_run_artifacts,
    verify_run_artifacts,
)

_KEY = b"artifact-test-hmac-key-0123456789"


def _sdd(tmp_path: Path) -> Path:
    sdd = tmp_path / ".sdd"
    sdd.mkdir()
    return sdd


class TestPayloadValidation:
    def test_report_requires_body(self) -> None:
        with pytest.raises(ArtifactValidationError):
            ArtifactPayload.report("")

    def test_table_row_width_must_match_columns(self) -> None:
        with pytest.raises(ArtifactValidationError):
            ArtifactPayload.table(["a", "b"], [["only-one"]])

    def test_link_kind_must_be_declared(self) -> None:
        with pytest.raises(ArtifactValidationError):
            ArtifactPayload.link("https://x", "unknown-kind")

    def test_report_canonical_bytes_are_stable(self) -> None:
        p = ArtifactPayload.report("# Title\nbody")
        assert p.canonical_bytes() == b'{"body":"# Title\\nbody","type":"report"}'


class TestPostRoundtrip:
    def test_post_report_produces_verifiable_record(self, tmp_path: Path) -> None:
        sdd = _sdd(tmp_path)
        rec = post_run_artifact(
            sdd_dir=sdd,
            task_id="task-1",
            key="audit-summary",
            payload=ArtifactPayload.report("# Findings\nAll clear."),
            actor="worker-a",
            hmac_key=_KEY,
        )
        assert rec.version == 1
        assert rec.prev_version_hash == ""
        assert rec.content_hash.startswith("sha256:")
        assert rec.spine_entry_hash.startswith("sha256:")
        assert rec.journal_index == 0

        results = verify_run_artifacts(sdd, "task-1", hmac_key=_KEY)
        assert len(results) == 1
        assert results[0].ok, results[0].reason

    def test_table_and_link_roundtrip(self, tmp_path: Path) -> None:
        sdd = _sdd(tmp_path)
        post_run_artifact(
            sdd_dir=sdd,
            task_id="task-1",
            key="comparison",
            payload=ArtifactPayload.table(["metric", "before", "after"], [["p95", "120", "80"]]),
            actor="worker-a",
            hmac_key=_KEY,
        )
        link = post_run_artifact(
            sdd_dir=sdd,
            task_id="task-1",
            key="preview",
            payload=ArtifactPayload.link("https://preview.example/xyz", "preview"),
            actor="worker-a",
            hmac_key=_KEY,
        )
        assert link.link_kind == "preview"
        results = verify_run_artifacts(sdd, "task-1", hmac_key=_KEY)
        assert len(results) == 2
        assert all(r.ok for r in results)


class TestVersionChaining:
    def test_reposting_a_key_chains_to_the_prior_version(self, tmp_path: Path) -> None:
        sdd = _sdd(tmp_path)
        v1 = post_run_artifact(
            sdd_dir=sdd,
            task_id="task-1",
            key="report",
            payload=ArtifactPayload.report("draft one"),
            actor="w",
            hmac_key=_KEY,
        )
        v2 = post_run_artifact(
            sdd_dir=sdd,
            task_id="task-1",
            key="report",
            payload=ArtifactPayload.report("draft two"),
            actor="w",
            hmac_key=_KEY,
        )
        assert v2.version == 2
        assert v2.prev_version_hash == v1.spine_entry_hash

        # History renders in order; both versions verify independently.
        rows = read_artifact_rows(sdd, "task-1")
        assert [r.version for r in rows] == [1, 2]
        results = verify_run_artifacts(sdd, "task-1", hmac_key=_KEY)
        assert len(results) == 2
        assert all(r.ok for r in results)

        # latest_versions reports v2 as the current version of the key.
        assert latest_versions(sdd, "task-1")["report"].version == 2


class TestTamperEvidence:
    def test_flipped_blob_byte_fails_verification_naming_the_key(self, tmp_path: Path) -> None:
        sdd = _sdd(tmp_path)
        rec = post_run_artifact(
            sdd_dir=sdd,
            task_id="task-1",
            key="audit-summary",
            payload=ArtifactPayload.report("# original"),
            actor="w",
            hmac_key=_KEY,
        )
        # Flip a byte in the stored blob (the file is addressed by the ORIGINAL
        # hash, so rehashing the tampered bytes will not match the journal row).
        store = EvidenceStore(sdd / "evidence")
        blob_path = store.blob_path(rec.content_hash)
        data = bytearray(blob_path.read_bytes())
        data[0] ^= 0x01
        blob_path.write_bytes(bytes(data))

        results = verify_run_artifacts(sdd, "task-1", hmac_key=_KEY)
        assert len(results) == 1
        assert not results[0].ok
        assert "audit-summary" in results[0].reason
        assert "index=0" in results[0].reason

    def test_flipped_journal_row_fails_verification(self, tmp_path: Path) -> None:
        sdd = _sdd(tmp_path)
        post_run_artifact(
            sdd_dir=sdd,
            task_id="task-1",
            key="korig",
            payload=ArtifactPayload.report("body"),
            actor="w",
            hmac_key=_KEY,
        )
        journal_path = sdd / "runs" / "task-task-1" / "journal.jsonl"
        text = journal_path.read_text(encoding="utf-8")
        # Corrupt the recorded key without fixing the Merkle chain. The journal
        # recomputes each payload hash, so this breaks the chain at the row.
        forged = text.replace('"korig"', '"forged"')
        assert forged != text, "expected the key value to be present for tampering"
        journal_path.write_text(forged, encoding="utf-8")

        results = verify_run_artifacts(sdd, "task-1", hmac_key=_KEY)
        assert results
        assert not any(r.ok for r in results)
        # The diagnostic names the (forged) key and the exact journal position.
        reason = results[0].reason
        assert "forged" in reason
        assert "index=0" in reason

    def test_tamper_hiding_every_row_still_fails(self, tmp_path: Path) -> None:
        # Renaming the artifact_posted event breaks the Merkle chain AND removes
        # the row from the artifact reader. Verification must still fail loudly
        # rather than read as "no artifacts" (a clean, empty set).
        sdd = _sdd(tmp_path)
        post_run_artifact(
            sdd_dir=sdd,
            task_id="task-1",
            key="k",
            payload=ArtifactPayload.report("body"),
            actor="w",
            hmac_key=_KEY,
        )
        journal_path = sdd / "runs" / "task-task-1" / "journal.jsonl"
        text = journal_path.read_text(encoding="utf-8")
        journal_path.write_text(text.replace("artifact_posted", "artifact_hidden"), encoding="utf-8")

        results = verify_run_artifacts(sdd, "task-1", hmac_key=_KEY)
        assert results, "tampering that hides all rows must still produce a verdict"
        assert not any(r.ok for r in results)
        assert "does not verify" in results[0].reason

        # verify_all also surfaces the hidden-row tamper for audit verify.
        all_results = verify_all_run_artifacts(tmp_path, hmac_key=_KEY)
        assert all_results
        assert not any(r.ok for r in all_results)


class TestConcurrency:
    def test_concurrent_posts_allocate_unique_versions(self, tmp_path: Path) -> None:
        # Serialised version allocation: N threads posting to the same (task,key)
        # must produce a clean 1..N version chain, never a duplicate version or a
        # forked predecessor reference.
        import threading

        sdd = _sdd(tmp_path)
        n = 12
        barrier = threading.Barrier(n)
        results: list[object] = []
        results_lock = threading.Lock()

        def _worker(i: int) -> None:
            barrier.wait()  # maximise contention
            rec = post_run_artifact(
                sdd_dir=sdd,
                task_id="task-1",
                key="k",
                payload=ArtifactPayload.report(f"draft {i}"),
                actor="w",
                hmac_key=_KEY,
            )
            with results_lock:
                results.append(rec)

        threads = [threading.Thread(target=_worker, args=(i,)) for i in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        rows = read_artifact_rows(sdd, "task-1")
        versions = sorted(r.version for r in rows)
        assert versions == list(range(1, n + 1)), f"versions not a clean 1..N chain: {versions}"
        # Every version except v1 references exactly the prior version's identity.
        by_version = {r.version: r for r in rows}
        for v in range(2, n + 1):
            assert by_version[v].prev_version_hash == by_version[v - 1].spine_entry_hash
        # The whole chain verifies.
        verdicts = verify_run_artifacts(sdd, "task-1", hmac_key=_KEY)
        assert len(verdicts) == n
        assert all(x.ok for x in verdicts)


class TestCap:
    def test_oversized_payload_is_rejected_naming_the_cap(self, tmp_path: Path) -> None:
        sdd = _sdd(tmp_path)
        with pytest.raises(ArtifactTooLargeError) as exc:
            post_run_artifact(
                sdd_dir=sdd,
                task_id="task-1",
                key="big",
                payload=ArtifactPayload.report("x" * 200),
                actor="w",
                hmac_key=_KEY,
                max_blob_bytes=64,
            )
        assert "64" in str(exc.value)
        assert exc.value.cap == 64


class TestGcLiveness:
    def test_gc_never_removes_a_live_artifact_blob(self, tmp_path: Path) -> None:
        sdd = _sdd(tmp_path)
        rec = post_run_artifact(
            sdd_dir=sdd,
            task_id="task-1",
            key="k",
            payload=ArtifactPayload.report("keep me"),
            actor="w",
            hmac_key=_KEY,
        )
        store = EvidenceStore(sdd / "evidence")
        assert store.has(rec.content_hash)

        # gc with the artifact's live hashes keeps the blob.
        removed = store.gc(live_artifact_content_hashes(sdd))
        assert removed == 0
        assert store.has(rec.content_hash)

        # gc with an empty live set would collect it (control).
        store.gc(set())
        assert not store.has(rec.content_hash)

    def test_superseded_versions_stay_live(self, tmp_path: Path) -> None:
        sdd = _sdd(tmp_path)
        v1 = post_run_artifact(
            sdd_dir=sdd,
            task_id="task-1",
            key="k",
            payload=ArtifactPayload.report("draft one"),
            actor="w",
            hmac_key=_KEY,
        )
        v2 = post_run_artifact(
            sdd_dir=sdd,
            task_id="task-1",
            key="k",
            payload=ArtifactPayload.report("draft two"),
            actor="w",
            hmac_key=_KEY,
        )
        store = EvidenceStore(sdd / "evidence")
        store.gc(live_artifact_content_hashes(sdd))
        # Both the superseded v1 and current v2 blobs survive.
        assert store.has(v1.content_hash)
        assert store.has(v2.content_hash)


class TestAuditMirror:
    def test_post_mirrors_into_the_audit_chain(self, tmp_path: Path) -> None:
        from bernstein.core.security.audit_chain import EVENT_RUN_ARTIFACT, AuditChainStore

        sdd = _sdd(tmp_path)
        chain = AuditChainStore(sdd / "audit", key=_KEY)
        rec = post_run_artifact(
            sdd_dir=sdd,
            task_id="task-1",
            key="k",
            payload=ArtifactPayload.report("body"),
            actor="worker-a",
            hmac_key=_KEY,
            audit_chain=chain,
        )
        events = chain.query(event_type=EVENT_RUN_ARTIFACT)
        assert len(events) == 1
        assert events[0].resource_id == rec.spine_entry_hash
        assert events[0].details["content_hash"] == rec.content_hash
        ok, errors = chain.verify()
        assert ok, errors


class TestVerifyAll:
    def test_verify_all_walks_every_task(self, tmp_path: Path) -> None:
        sdd = _sdd(tmp_path)
        for task in ("task-1", "task-2"):
            post_run_artifact(
                sdd_dir=sdd,
                task_id=task,
                key="k",
                payload=ArtifactPayload.report(f"body for {task}"),
                actor="w",
                hmac_key=_KEY,
            )
        results = verify_all_run_artifacts(tmp_path, hmac_key=_KEY)
        assert len(results) == 2
        assert all(r.ok for r in results)


class TestTaskIdPathContainment:
    """A task id reaches these readers from a request path, a CLI argument, or
    a journal row, and every one of them turns it into a filesystem path."""

    @pytest.mark.parametrize(
        "task_id",
        [
            "../../../../etc/passwd",
            "../outside",
            "task/../../escape",
            "/absolute/path",
            "task\x00null",
            "task\r\nid",
        ],
    )
    def test_traversal_task_id_reads_as_empty_and_touches_nothing_outside(self, tmp_path: Path, task_id: str) -> None:
        """Readers absorb an id that names no task; nothing outside is touched.

        The readers are called with arbitrary CLI arguments, so an id they
        cannot resolve reads as "no artifacts" rather than raising. The
        security property is unchanged and asserted here directly: no path
        outside the runs directory is read, created, or removed.
        """
        sdd = _sdd(tmp_path)
        canary = tmp_path / "outside.jsonl"
        canary.write_text('{"event":"artifact_posted"}\n', encoding="utf-8")
        before = sorted(p.name for p in tmp_path.iterdir())

        assert read_artifact_rows(sdd, task_id) == []
        assert verify_run_artifacts(sdd, task_id, hmac_key=_KEY) == []
        assert latest_versions(sdd, task_id) == {}

        assert canary.read_text(encoding="utf-8") == '{"event":"artifact_posted"}\n'
        assert not (tmp_path / "etc").exists()
        assert sorted(p.name for p in tmp_path.iterdir()) == before

    @pytest.mark.parametrize(
        "task_id",
        [
            "../../../../etc/passwd",
            "../outside",
            "task/../../escape",
            "/absolute/path",
            "task\x00null",
            "task\r\nid",
            "task-1\n",
        ],
    )
    def test_the_path_helper_refuses_a_traversal_id_with_a_typed_error(self, tmp_path: Path, task_id: str) -> None:
        """The typed refusal still exists at the boundary; the public readers
        choose to absorb it, and the writer lets it propagate."""
        from bernstein.core.evidence.run_artifacts import _artifact_journal_path

        with pytest.raises(ArtifactValidationError):
            _artifact_journal_path(_sdd(tmp_path), task_id)

    @pytest.mark.parametrize("task_id", ["..", ".", "..."])
    def test_dot_segment_task_id_stays_inside_the_runs_dir(self, tmp_path: Path, task_id: str) -> None:
        """A dot-segment id is admitted by the alphabet but cannot escape.

        ``task_run_id`` prefixes every id, so ``..`` addresses the literal
        directory ``task-..`` rather than the parent. The containment check is
        what guarantees this, so assert containment directly rather than
        asserting a refusal the reader does not owe.
        """
        from bernstein.core.evidence.run_artifacts import _artifact_journal_path

        sdd = _sdd(tmp_path)
        resolved = _artifact_journal_path(sdd, task_id)
        assert resolved.is_relative_to((sdd / "runs").resolve())
        assert read_artifact_rows(sdd, task_id) == []

    def test_validation_performs_no_filesystem_access(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Regression guard against reintroducing ``Path.resolve()``.

        Not evidence that containment works - the sibling tests carry that and
        fail when `_artifact_journal_path` is reverted. This guards the
        narrower property that containment is decided without touching disk,
        which an earlier revision of this fix got wrong. Asserted by making
        filesystem access explode, because a planted-symlink test passes on any
        implementation that happens not to resolve.
        """
        import os.path

        from bernstein.core.evidence.run_artifacts import _artifact_journal_path

        def _boom(*_args: object, **_kwargs: object) -> Path:
            raise AssertionError("_artifact_journal_path must not touch the filesystem to decide containment")

        monkeypatch.setattr(Path, "resolve", _boom)
        monkeypatch.setattr(os.path, "realpath", _boom)

        sdd = _sdd(tmp_path)
        assert _artifact_journal_path(sdd, "task-1").name == "journal.jsonl"
        with pytest.raises(ArtifactValidationError):
            _artifact_journal_path(sdd, "../../escape")

    @pytest.mark.parametrize("task_id", ["task-1\n", "report\n", "t\n"])
    def test_trailing_newline_task_id_is_refused(self, tmp_path: Path, task_id: str) -> None:
        """Python's `$` also matches before a trailing newline, so this shape
        passed the alphabet check until the anchor became `\\Z`."""
        from bernstein.core.evidence.run_artifacts import _artifact_journal_path

        sdd = _sdd(tmp_path)
        with pytest.raises(ArtifactValidationError):
            _artifact_journal_path(sdd, task_id)

    def test_traversal_task_id_writes_nothing_outside_the_runs_dir(self, tmp_path: Path) -> None:
        sdd = _sdd(tmp_path)
        before = sorted(p.name for p in tmp_path.iterdir())
        with pytest.raises(ArtifactValidationError):
            post_run_artifact(
                sdd_dir=sdd,
                task_id="../../escape",
                key="k",
                payload=ArtifactPayload.report("body"),
                actor="w",
                hmac_key=_KEY,
            )
        assert sorted(p.name for p in tmp_path.iterdir()) == before

    def test_valid_task_id_still_resolves_inside_the_runs_dir(self, tmp_path: Path) -> None:
        sdd = _sdd(tmp_path)
        rec = post_run_artifact(
            sdd_dir=sdd,
            task_id="task-1",
            key="k",
            payload=ArtifactPayload.report("body"),
            actor="w",
            hmac_key=_KEY,
        )
        assert rec.version == 1
        assert read_artifact_rows(sdd, "task-1")[0].content_hash == rec.content_hash

    def test_rewritten_task_id_row_is_reported_not_skipped(self, tmp_path: Path) -> None:
        """A row whose task id no longer maps to its own journal is tampering.

        Verifying under the rewritten id would read a different (or absent)
        journal and report a clean, empty result for a tampered run.
        """
        sdd = _sdd(tmp_path)
        post_run_artifact(
            sdd_dir=sdd,
            task_id="task-1",
            key="k",
            payload=ArtifactPayload.report("body"),
            actor="w",
            hmac_key=_KEY,
        )
        journal = sdd / "runs" / "task-task-1" / "journal.jsonl"
        journal.write_text(
            journal.read_text(encoding="utf-8").replace('"task_id": "task-1"', '"task_id": "../elsewhere"'),
            encoding="utf-8",
        )
        results = verify_all_run_artifacts(tmp_path, hmac_key=_KEY)
        assert results, "a rewritten task id must not silently verify as an empty artifact set"
        assert all(not r.ok for r in results)
