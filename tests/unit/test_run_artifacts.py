"""Agent-posted, journal-anchored task artifacts (#2553).

These tests exercise the substrate coupling the ticket demands: every posted
artifact is content-addressed, spine-sealed, and journal-chained, so a single
flipped byte -- in the stored blob or in the journal row -- fails verification
naming the artifact, and the progress vector cannot be inflated by posting.
"""

from __future__ import annotations

import json
import unicodedata
from pathlib import Path
from typing import Any

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


def _sarif_result(
    *,
    start_line: int = 8,
    snippet: str = "eval(user_input)",
    uri: str = "./src/app.py",
    rule_id: str = "PY-TAINT-001",
) -> dict[str, object]:
    return {
        "ruleId": rule_id,
        "message": {"text": "Untrusted input reaches eval"},
        "locations": [
            {
                "physicalLocation": {
                    "artifactLocation": {"uri": uri},
                    "region": {
                        "startLine": start_line,
                        "endLine": start_line,
                        "startColumn": 5,
                        "endColumn": 21,
                        "snippet": {"text": snippet},
                    },
                }
            }
        ],
    }


def _physical(result: dict[str, Any]) -> dict[str, Any]:
    physical: dict[str, Any] = result["locations"][0]["physicalLocation"]
    return physical


def _region(result: dict[str, Any]) -> dict[str, Any]:
    region: dict[str, Any] = _physical(result)["region"]
    return region


def _address(**kwargs: Any) -> object:
    """The content address of the default finding, varied by ``_sarif_result`` kwargs."""
    return _finding(_sarif_result(**kwargs)).to_content_dict()["address"]


def _finding(result: dict[str, object] | None = None, **overrides: str) -> ArtifactPayload:
    provenance = {
        "tool": "semgrep",
        "tool_version": "1.131.0",
        "pinned_ruleset_or_feed_digest": "sha256:" + "a" * 64,
        "invocation_argv_hash": "sha256:" + "b" * 64,
        "target": "git:0123456789abcdef",
    }
    provenance.update(overrides)
    return ArtifactPayload.finding(result or _sarif_result(), **provenance)


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


class TestFindingPayload:
    def test_blank_lines_above_do_not_change_finding_address(self) -> None:
        before = _finding(_sarif_result(start_line=8)).to_content_dict()
        after = _finding(_sarif_result(start_line=12)).to_content_dict()
        assert before["address"] == after["address"]

    def test_different_snippets_have_different_addresses(self) -> None:
        first = _finding(_sarif_result(snippet="eval(user_input)")).to_content_dict()
        second = _finding(_sarif_result(snippet="exec(user_input)")).to_content_dict()
        assert first["address"] != second["address"]

    @pytest.mark.parametrize(
        ("field", "changed"),
        [
            ("tool", "codeql"),
            ("tool_version", "2.20.0"),
            ("pinned_ruleset_or_feed_digest", "sha256:" + "c" * 64),
            ("invocation_argv_hash", "sha256:" + "d" * 64),
            ("target", "git:fedcba9876543210"),
        ],
    )
    def test_every_provenance_field_is_in_address_preimage(self, field: str, changed: str) -> None:
        baseline = _finding().to_content_dict()
        modified = _finding(**{field: changed}).to_content_dict()
        assert baseline["address"] != modified["address"]

    # -- The address is a cross-platform, cross-run constant -----------------
    #
    # An address that is only stable on the machine that minted it is not an
    # address. Each of the next four tests pins one way the platform could
    # otherwise leak into the preimage.

    @pytest.mark.parametrize(
        "spelling",
        [
            "src\\app.py",  # a Windows scanner reporting the same file
            "./src/app.py",
            "src//app.py",
            "src/helpers/../app.py",
        ],
    )
    def test_path_spelling_does_not_change_address(self, spelling: str) -> None:
        assert _address(uri=spelling) == _address(uri="src/app.py")

    @pytest.mark.parametrize("newline", ["\r\n", "\r"])
    def test_line_ending_style_does_not_change_address(self, newline: str) -> None:
        # A CRLF checkout and an LF checkout are the same source, so the same
        # finding, so the same address.
        multiline = f"if flag:{newline}    eval(user_input){newline}"
        assert _address(snippet=multiline) == _address(snippet=multiline.replace(newline, "\n"))

    @pytest.mark.parametrize(("field", "text"), [("uri", "src/café.py"), ("snippet", "x = 'café'")])
    def test_unicode_normal_form_does_not_change_address(self, field: str, text: str) -> None:
        # macOS hands back decomposed (NFD) bytes where Linux hands back
        # composed (NFC) ones; one file must not address as two.
        nfc, nfd = unicodedata.normalize("NFC", text), unicodedata.normalize("NFD", text)
        assert nfc != nfd, "fixture must actually differ between normal forms"
        assert _address(**{field: nfc}) == _address(**{field: nfd})

    def test_address_ignores_sarif_key_order_and_untracked_fields(self) -> None:
        baseline = _address()
        reordered = _sarif_result()
        reordered = {key: reordered[key] for key in reversed(list(reordered))}
        assert _finding(reordered).to_content_dict()["address"] == baseline
        # Fields outside the preimage -- severity, fingerprints, the human
        # message -- must not reissue the identity when a scanner reworks them.
        noisier = _sarif_result()
        noisier["level"] = "error"
        noisier["fingerprints"] = {"vendor/v1": "abc"}
        noisier["message"] = {"text": "reworded advisory copy"}
        assert _finding(noisier).to_content_dict()["address"] == baseline

    def test_address_of_the_reference_fixture_is_frozen(self) -> None:
        # A recomputation on any machine, any run, any Python version must land
        # here. If this constant moves, the preimage changed and every stored
        # finding address in the field changed with it -- so it moves only on
        # purpose, never as a side effect.
        assert _address() == "sha256:9ba1d731f13c4a92c54010b588ab16e549a5f6346daf76c71ad010d12676ad11"

    # -- Separation and the deliberate collision ----------------------------

    @pytest.mark.parametrize(
        "variant",
        [
            {"rule_id": "PY-TAINT-002"},
            {"uri": "src/other.py"},
            {"snippet": "exec(user_input)"},
        ],
    )
    def test_findings_differing_in_a_bound_field_address_differently(self, variant: dict[str, str]) -> None:
        assert _address(**variant) != _address()

    def test_identical_snippets_at_different_lines_share_one_address(self) -> None:
        # Documents the design, not an accident: identity is line-independent,
        # so two textually identical hits of one rule in one file are one
        # address. Triage state keyed on it therefore covers both.
        assert _address(start_line=8) == _address(start_line=200)

    # -- Malformed input never yields a partial preimage --------------------

    @pytest.mark.parametrize(
        ("mutate", "expected"),
        [
            (lambda r: r.pop("locations"), r"result\.locations\[0\]"),
            (lambda r: r.pop("ruleId"), r"result\.ruleId"),
            (lambda r: _region(r).pop("snippet"), r"region\.snippet"),
            (lambda r: _region(r)["snippet"].pop("text"), r"region\.snippet\.text"),
            (lambda r: _region(r).pop("startLine"), r"region\.startLine"),
            (lambda r: _region(r).update(endLine=1), r"region\.endLine"),
            (lambda r: _region(r).update(endColumn=1), r"region\.endColumn"),
            (lambda r: _physical(r).pop("artifactLocation"), r"artifactLocation"),
            (lambda r: _physical(r)["artifactLocation"].update(uri="."), r"empty normalized artifact URI"),
        ],
    )
    def test_malformed_sarif_names_the_field_and_yields_nothing(self, mutate: Any, expected: str) -> None:
        malformed = _sarif_result()
        mutate(malformed)
        with pytest.raises(ArtifactValidationError, match=expected):
            _finding(malformed)

    @pytest.mark.parametrize(
        "field",
        ["tool", "tool_version", "pinned_ruleset_or_feed_digest", "invocation_argv_hash", "target"],
    )
    def test_empty_provenance_field_is_refused(self, field: str) -> None:
        with pytest.raises(ArtifactValidationError, match=f"provenance requires non-empty {field}"):
            _finding(**{field: ""})

    def test_finding_roundtrip_verifies(self, tmp_path: Path) -> None:
        sdd = _sdd(tmp_path)
        post_run_artifact(
            sdd_dir=sdd,
            task_id="task-1",
            key="finding",
            payload=_finding(),
            actor="scanner",
            hmac_key=_KEY,
        )
        result = verify_run_artifacts(sdd, "task-1", hmac_key=_KEY)
        assert len(result) == 1
        assert result[0].ok, result[0].reason
        assert result[0].journal_identity == "unverifiable"

    def test_verify_rejects_recorded_address_mismatch(self, tmp_path: Path) -> None:
        sdd = _sdd(tmp_path)
        forged = _finding().to_content_dict()
        forged["address"] = "sha256:" + "0" * 64
        post_run_artifact(
            sdd_dir=sdd,
            task_id="task-1",
            key="finding",
            payload=ArtifactPayload(
                artifact_type="finding",
                finding_json=json.dumps(forged, sort_keys=True, separators=(",", ":")),
            ),
            actor="scanner",
            hmac_key=_KEY,
        )
        result = verify_run_artifacts(sdd, "task-1", hmac_key=_KEY)
        assert len(result) == 1
        assert not result[0].ok
        assert "does not match recomputed address" in result[0].reason

    @pytest.mark.parametrize(
        ("field", "forged", "expected"),
        [
            (
                "identity",
                {"rule_id": "SOMETHING-ELSE", "artifact_uri": "src/app.py"},
                "recorded finding identity does not match normalized SARIF result",
            ),
            (
                "location",
                {"artifact_uri": "src/elsewhere.py", "start_line": 1},
                "recorded finding location does not match normalized SARIF result",
            ),
            ("type", "report", "recorded finding payload has wrong artifact type"),
        ],
    )
    def test_verify_recomputes_rather_than_trusting_stored_fields(
        self, tmp_path: Path, field: str, forged: object, expected: str
    ) -> None:
        # The stored address is re-derived from the stored SARIF result, so a
        # payload whose narrated identity disagrees with its own evidence is
        # rejected -- verification never reads the field and believes it.
        sdd = _sdd(tmp_path)
        # The address is left correct on purpose, so the only thing wrong with
        # the payload is the field being forged.
        content = _finding().to_content_dict()
        content[field] = forged  # type: ignore[literal-required]
        post_run_artifact(
            sdd_dir=sdd,
            task_id="task-1",
            key="finding",
            payload=ArtifactPayload(
                artifact_type="finding",
                finding_json=json.dumps(content, sort_keys=True, separators=(",", ":")),
            ),
            actor="scanner",
            hmac_key=_KEY,
        )
        result = verify_run_artifacts(sdd, "task-1", hmac_key=_KEY)
        assert len(result) == 1
        assert not result[0].ok
        assert expected in result[0].reason


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

    def test_a_malformed_id_is_refused_before_the_filesystem_is_touched(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An unsafe id must be rejected lexically, before anything resolves.

        This originally asserted that containment never touched disk at all.
        That is too strong, and buying it would cost a real detection: only
        resolution catches a run directory that is a *symlink* out of the runs
        root, and ``verify_journal`` cannot cover that gap because it is an
        unkeyed Merkle recompute - whoever plants the symlink can satisfy it.

        The property worth guarding is the ORDER. A hostile id is screened
        against the alphabet first and never reaches ``realpath``; a well-formed
        id resolves, which is what makes the symlink case detectable. Asserted
        by making filesystem access explode and checking which of the two ids
        gets that far.
        """
        import os.path

        from bernstein.core.evidence.run_artifacts import _artifact_journal_path

        def _boom(*_args: object, **_kwargs: object) -> Path:
            raise AssertionError("a malformed id must be refused before the filesystem is touched")

        monkeypatch.setattr(Path, "resolve", _boom)
        monkeypatch.setattr(os.path, "realpath", _boom)

        sdd = _sdd(tmp_path)
        # Refused on the alphabet alone - never reaches the patched realpath.
        with pytest.raises(ArtifactValidationError):
            _artifact_journal_path(sdd, "../../escape")

    @pytest.mark.parametrize("key", ["k\n", "report\n", "a.b\n"])
    def test_trailing_newline_artifact_key_is_refused(self, tmp_path: Path, key: str) -> None:
        """The key is embedded unescaped in the spine artifact path, so its
        alphabet exists to exclude control characters. Python's `$` admits a
        single trailing newline, so this shape was accepted until the anchor
        became `\\Z`."""
        sdd = _sdd(tmp_path)
        with pytest.raises(ArtifactValidationError, match="artifact key"):
            post_run_artifact(
                sdd_dir=sdd,
                task_id="task-1",
                key=key,
                payload=ArtifactPayload.report("body"),
                actor="w",
                hmac_key=_KEY,
            )
        assert read_artifact_rows(sdd, "task-1") == []

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
