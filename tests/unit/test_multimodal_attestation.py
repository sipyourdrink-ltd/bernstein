"""Unit tests for image-attachment passthrough with provenance (#1797).

Covers:
* Task model accepts an ``attachments`` list.
* Spawn-time wiring: :func:`build_attachment_context` reads paths, stores
  bytes in CAS, builds a :class:`MultiModalContext`, and records an
  audit-chain entry of type ``multimodal.attach``.
* The lineage helper exposes attachment digests as artefact parents.
* Capability gating refuses adapters that do not advertise multimodal
  support, with a structured error suggesting capable adapters.
* sha256 stability: encoding the same bytes always yields the same digest.
* Worktree pinning: an image attached in worktree wt-a is not reachable
  from a worker in wt-b.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from bernstein.core.agents.multimodal import (
    ModalityType,
    MultiModalContext,
    is_multimodal_capable,
)
from bernstein.core.agents.multimodal_attestation import (
    AttachmentResolution,
    CapabilityRefusal,
    WorktreeAccessDenied,
    build_attachment_context,
    refuse_when_incapable,
    resolve_attachment_for_worker,
    worker_lineage_parents,
)
from bernstein.core.persistence.cas_store import CASStore
from bernstein.core.security.audit_chain import (
    EVENT_MULTIMODAL_ATTACH,
    AuditChainStore,
    record_multimodal_attach,
)
from bernstein.core.tasks.models import Task

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def _make_image(path: Path, payload: bytes = PNG_MAGIC + b"hello") -> Path:
    path.write_bytes(payload)
    return path


def _audit_chain(tmp_path: Path) -> AuditChainStore:
    return AuditChainStore(
        audit_dir=tmp_path / "audit",
        key=b"k" * 32,
    )


# ---------------------------------------------------------------------------
# Task model field
# ---------------------------------------------------------------------------


class TestTaskAttachmentsField:
    def test_task_accepts_attachments_default_empty(self) -> None:
        task = Task(
            id="t-1",
            title="With attachment",
            description="desc",
            role="backend",
        )
        assert task.attachments == []

    def test_task_accepts_attachments_list(self, tmp_path: Path) -> None:
        img = _make_image(tmp_path / "shot.png")
        task = Task(
            id="t-2",
            title="With attachment",
            description="desc",
            role="backend",
            attachments=[str(img)],
        )
        assert task.attachments == [str(img)]

    def test_task_from_dict_reads_attachments(self, tmp_path: Path) -> None:
        img = _make_image(tmp_path / "shot.png")
        raw = {
            "id": "t-3",
            "title": "x",
            "description": "y",
            "role": "backend",
            "attachments": [str(img)],
        }
        task = Task.from_dict(raw)
        assert task.attachments == [str(img)]


# ---------------------------------------------------------------------------
# Capability gating
# ---------------------------------------------------------------------------


class TestCapabilityGating:
    def test_capable_adapter_passes(self, tmp_path: Path) -> None:
        img = _make_image(tmp_path / "shot.png")
        # No exception raised.
        refuse_when_incapable(adapter_name="claude", attachments=[str(img)])

    def test_incapable_adapter_refused(self, tmp_path: Path) -> None:
        img = _make_image(tmp_path / "shot.png")
        with pytest.raises(CapabilityRefusal) as exc:
            refuse_when_incapable(adapter_name="codex", attachments=[str(img)])
        assert "codex" in str(exc.value).lower()
        # Error suggests adapters that DO support attachments.
        assert exc.value.suggested_adapters
        assert all(is_multimodal_capable(a) for a in exc.value.suggested_adapters)

    def test_no_attachments_no_refusal(self) -> None:
        # An incapable adapter is fine when no attachments are present.
        refuse_when_incapable(adapter_name="codex", attachments=[])


# ---------------------------------------------------------------------------
# sha256 stability
# ---------------------------------------------------------------------------


class TestSha256Stability:
    def test_same_bytes_same_digest(self, tmp_path: Path) -> None:
        img1 = _make_image(tmp_path / "a.png", payload=b"identical")
        img2 = _make_image(tmp_path / "b.png", payload=b"identical")
        chain1 = _audit_chain(tmp_path / "x1")
        chain2 = _audit_chain(tmp_path / "x2")
        cas1 = CASStore(tmp_path / "cas1")
        cas2 = CASStore(tmp_path / "cas2")

        ctx1 = build_attachment_context(
            attachments=[str(img1)],
            worker_id="wkr-1",
            turn_seq=1,
            worktree_id="wt-a",
            cas=cas1,
            audit_chain=chain1,
        )
        ctx2 = build_attachment_context(
            attachments=[str(img2)],
            worker_id="wkr-2",
            turn_seq=1,
            worktree_id="wt-b",
            cas=cas2,
            audit_chain=chain2,
        )
        assert ctx1.resolutions[0].sha256 == ctx2.resolutions[0].sha256


# ---------------------------------------------------------------------------
# build_attachment_context
# ---------------------------------------------------------------------------


class TestBuildAttachmentContext:
    def test_returns_multimodal_context(self, tmp_path: Path) -> None:
        img = _make_image(tmp_path / "shot.png")
        chain = _audit_chain(tmp_path)
        cas = CASStore(tmp_path / "cas")
        result = build_attachment_context(
            attachments=[str(img)],
            worker_id="wkr-1",
            turn_seq=0,
            worktree_id="wt-a",
            cas=cas,
            audit_chain=chain,
        )
        assert isinstance(result.context, MultiModalContext)
        assert result.context.primary_modality == ModalityType.IMAGE
        assert len(result.context.inputs) == 1
        assert result.context.inputs[0].mime_type == "image/png"

    def test_records_audit_chain_entry(self, tmp_path: Path) -> None:
        img = _make_image(tmp_path / "shot.png")
        chain = _audit_chain(tmp_path)
        cas = CASStore(tmp_path / "cas")
        result = build_attachment_context(
            attachments=[str(img)],
            worker_id="wkr-1",
            turn_seq=7,
            worktree_id="wt-a",
            cas=cas,
            audit_chain=chain,
        )
        # Find the multimodal.attach event in the chain.
        entries = chain.query(event_type=EVENT_MULTIMODAL_ATTACH)
        assert len(entries) == 1
        details = entries[0].details
        assert details["sha256"] == result.resolutions[0].sha256
        assert details["mime"] == "image/png"
        assert details["worker_id"] == "wkr-1"
        assert details["turn_seq"] == 7
        assert details["worktree_id"] == "wt-a"
        # operator_install_id_sig is recorded (non-empty string).
        assert details["operator_install_id_sig"]
        assert details["prev_chain_digest"]

    def test_stores_bytes_in_cas(self, tmp_path: Path) -> None:
        payload = PNG_MAGIC + b"unique-bytes"
        img = _make_image(tmp_path / "shot.png", payload=payload)
        chain = _audit_chain(tmp_path)
        cas = CASStore(tmp_path / "cas")
        result = build_attachment_context(
            attachments=[str(img)],
            worker_id="wkr-1",
            turn_seq=0,
            worktree_id="wt-a",
            cas=cas,
            audit_chain=chain,
        )
        digest = result.resolutions[0].sha256
        assert cas.has(digest)
        assert cas.get(digest) == payload

    def test_multiple_attachments(self, tmp_path: Path) -> None:
        i1 = _make_image(tmp_path / "a.png", payload=b"a" * 16)
        i2 = _make_image(tmp_path / "b.png", payload=b"b" * 16)
        chain = _audit_chain(tmp_path)
        cas = CASStore(tmp_path / "cas")
        result = build_attachment_context(
            attachments=[str(i1), str(i2)],
            worker_id="wkr-1",
            turn_seq=0,
            worktree_id="wt-a",
            cas=cas,
            audit_chain=chain,
        )
        assert len(result.resolutions) == 2
        entries = chain.query(event_type=EVENT_MULTIMODAL_ATTACH)
        assert len(entries) == 2

    def test_audit_chain_valid_after_attach(self, tmp_path: Path) -> None:
        img = _make_image(tmp_path / "shot.png")
        chain = _audit_chain(tmp_path)
        cas = CASStore(tmp_path / "cas")
        build_attachment_context(
            attachments=[str(img)],
            worker_id="wkr-1",
            turn_seq=0,
            worktree_id="wt-a",
            cas=cas,
            audit_chain=chain,
        )
        valid, errors = chain.verify()
        assert valid, f"chain integrity broken: {errors}"


# ---------------------------------------------------------------------------
# Lineage parent inclusion
# ---------------------------------------------------------------------------


class TestLineageParents:
    def test_resolutions_become_lineage_parents(self, tmp_path: Path) -> None:
        img = _make_image(tmp_path / "shot.png")
        chain = _audit_chain(tmp_path)
        cas = CASStore(tmp_path / "cas")
        result = build_attachment_context(
            attachments=[str(img)],
            worker_id="wkr-1",
            turn_seq=0,
            worktree_id="wt-a",
            cas=cas,
            audit_chain=chain,
        )
        parents = worker_lineage_parents(result)
        assert len(parents) == 1
        # Lineage parent identifiers are content-addressed.
        assert result.resolutions[0].sha256 in parents[0]

    def test_no_attachments_no_parents(self, tmp_path: Path) -> None:
        chain = _audit_chain(tmp_path)
        cas = CASStore(tmp_path / "cas")
        result = build_attachment_context(
            attachments=[],
            worker_id="wkr-1",
            turn_seq=0,
            worktree_id="wt-a",
            cas=cas,
            audit_chain=chain,
        )
        assert worker_lineage_parents(result) == []


# ---------------------------------------------------------------------------
# Worktree pinning
# ---------------------------------------------------------------------------


class TestWorktreePinning:
    def test_same_worktree_can_resolve(self, tmp_path: Path) -> None:
        img = _make_image(tmp_path / "shot.png")
        chain = _audit_chain(tmp_path)
        cas = CASStore(tmp_path / "cas")
        result = build_attachment_context(
            attachments=[str(img)],
            worker_id="wkr-1",
            turn_seq=0,
            worktree_id="wt-a",
            cas=cas,
            audit_chain=chain,
        )
        digest = result.resolutions[0].sha256
        # Same worktree id can resolve the attachment back to bytes.
        bytes_back = resolve_attachment_for_worker(
            sha256=digest,
            requesting_worktree_id="wt-a",
            cas=cas,
            audit_chain=chain,
        )
        assert bytes_back == img.read_bytes()

    def test_cross_worktree_rejected(self, tmp_path: Path) -> None:
        img = _make_image(tmp_path / "shot.png")
        chain = _audit_chain(tmp_path)
        cas = CASStore(tmp_path / "cas")
        result = build_attachment_context(
            attachments=[str(img)],
            worker_id="wkr-1",
            turn_seq=0,
            worktree_id="wt-a",
            cas=cas,
            audit_chain=chain,
        )
        digest = result.resolutions[0].sha256
        with pytest.raises(WorktreeAccessDenied):
            resolve_attachment_for_worker(
                sha256=digest,
                requesting_worktree_id="wt-b",
                cas=cas,
                audit_chain=chain,
            )


# ---------------------------------------------------------------------------
# Replay & tamper detection
# ---------------------------------------------------------------------------


class TestReplayAndTamper:
    def test_replay_reproduces_exact_bytes(self, tmp_path: Path) -> None:
        payload = PNG_MAGIC + b"exact-replay-bytes"
        img = _make_image(tmp_path / "shot.png", payload=payload)
        chain = _audit_chain(tmp_path)
        cas = CASStore(tmp_path / "cas")
        result = build_attachment_context(
            attachments=[str(img)],
            worker_id="wkr-1",
            turn_seq=0,
            worktree_id="wt-a",
            cas=cas,
            audit_chain=chain,
        )
        digest = result.resolutions[0].sha256
        replayed = resolve_attachment_for_worker(
            sha256=digest,
            requesting_worktree_id="wt-a",
            cas=cas,
            audit_chain=chain,
        )
        assert replayed == payload

    def test_tamper_breaks_verification(self, tmp_path: Path) -> None:
        img = _make_image(tmp_path / "shot.png")
        chain = _audit_chain(tmp_path)
        cas = CASStore(tmp_path / "cas")
        build_attachment_context(
            attachments=[str(img)],
            worker_id="wkr-1",
            turn_seq=0,
            worktree_id="wt-a",
            cas=cas,
            audit_chain=chain,
        )
        # Tamper with the audit chain log on disk.
        log_files = list((tmp_path / "audit").glob("*.jsonl"))
        assert log_files
        raw = log_files[0].read_bytes()
        # Flip one byte inside the JSON payload (not the trailing newline).
        # The first '{' is at offset 0; flip a hex character somewhere in
        # the middle of the line so the JSON still parses but the canonical
        # form drifts.
        tampered = bytearray(raw)
        # Find a digit / letter to flip in the payload.
        for i, b in enumerate(tampered):
            if chr(b).isalnum() and i > 0 and tampered[i - 1] != ord('"'):
                tampered[i] = ord("a") if chr(b) != "a" else ord("b")
                break
        log_files[0].write_bytes(bytes(tampered))
        valid, errors = chain.verify()
        assert not valid
        assert errors


# ---------------------------------------------------------------------------
# record_multimodal_attach helper
# ---------------------------------------------------------------------------


class TestRecordMultimodalAttach:
    def test_records_event_with_required_fields(self, tmp_path: Path) -> None:
        chain = _audit_chain(tmp_path)
        event = record_multimodal_attach(
            chain=chain,
            sha256="a" * 64,
            mime="image/png",
            operator_install_id_sig="install-sig-abc",
            worker_id="wkr-1",
            turn_seq=3,
            worktree_id="wt-a",
        )
        assert event.event_type == EVENT_MULTIMODAL_ATTACH
        assert event.details["sha256"] == "a" * 64
        assert event.details["mime"] == "image/png"
        assert event.details["worker_id"] == "wkr-1"
        assert event.details["turn_seq"] == 3
        assert event.details["worktree_id"] == "wt-a"
        assert event.details["operator_install_id_sig"] == "install-sig-abc"
        # prev_chain_digest comes from the underlying chain (genesis for first
        # event).
        assert event.details["prev_chain_digest"]

    def test_chain_continues_across_attaches(self, tmp_path: Path) -> None:
        chain = _audit_chain(tmp_path)
        e1 = record_multimodal_attach(
            chain=chain,
            sha256="a" * 64,
            mime="image/png",
            operator_install_id_sig="sig",
            worker_id="w",
            turn_seq=0,
            worktree_id="wt-a",
        )
        e2 = record_multimodal_attach(
            chain=chain,
            sha256="b" * 64,
            mime="image/jpeg",
            operator_install_id_sig="sig",
            worker_id="w",
            turn_seq=1,
            worktree_id="wt-a",
        )
        assert e2.details["prev_chain_digest"] == e1.hmac

    def test_query_filters_event_type(self, tmp_path: Path) -> None:
        chain = _audit_chain(tmp_path)
        # Mix in an unrelated event.
        chain.log(
            event_type="task.transition",
            actor="orchestrator",
            resource_type="task",
            resource_id="t-1",
        )
        record_multimodal_attach(
            chain=chain,
            sha256="a" * 64,
            mime="image/png",
            operator_install_id_sig="sig",
            worker_id="w",
            turn_seq=0,
            worktree_id="wt-a",
        )
        attaches = chain.query(event_type=EVENT_MULTIMODAL_ATTACH)
        assert len(attaches) == 1
        assert attaches[0].details["sha256"] == "a" * 64


# ---------------------------------------------------------------------------
# AttachmentResolution dataclass
# ---------------------------------------------------------------------------


class TestAttachmentResolution:
    def test_resolution_carries_digest_and_mime(self, tmp_path: Path) -> None:
        img = _make_image(tmp_path / "shot.png")
        chain = _audit_chain(tmp_path)
        cas = CASStore(tmp_path / "cas")
        result = build_attachment_context(
            attachments=[str(img)],
            worker_id="wkr-1",
            turn_seq=0,
            worktree_id="wt-a",
            cas=cas,
            audit_chain=chain,
        )
        r = result.resolutions[0]
        assert isinstance(r, AttachmentResolution)
        assert len(r.sha256) == 64
        assert r.mime == "image/png"
        assert r.worktree_id == "wt-a"


# ---------------------------------------------------------------------------
# YAML plan loader integration
# ---------------------------------------------------------------------------


class TestPlanLoaderAttachments:
    def test_yaml_plan_attachments_propagate(self, tmp_path: Path) -> None:
        img = _make_image(tmp_path / "img.png")
        plan_yaml = tmp_path / "plan.yaml"
        plan_yaml.write_text(
            "name: test\n"
            "stages:\n"
            "  - name: phase1\n"
            "    steps:\n"
            "      - title: With attachment\n"
            "        role: backend\n"
            f"        attachments: ['{img}']\n"
        )
        from bernstein.core.planning.plan_loader import load_plan_from_yaml

        tasks = load_plan_from_yaml(plan_yaml)
        assert len(tasks) == 1
        assert tasks[0].attachments == [str(img)]

    def test_yaml_plan_rejects_scalar_attachments(self, tmp_path: Path) -> None:
        """A scalar attachments value MUST surface a PlanLoadError."""
        img = _make_image(tmp_path / "img.png")
        plan_yaml = tmp_path / "plan.yaml"
        plan_yaml.write_text(
            "name: test\n"
            "stages:\n"
            "  - name: phase1\n"
            "    steps:\n"
            "      - title: With attachment\n"
            "        role: backend\n"
            f"        attachments: {img}\n"  # SCALAR (typo)
        )
        from bernstein.core.planning.plan_loader import PlanLoadError, load_plan_from_yaml

        with pytest.raises(PlanLoadError, match="attachments"):
            load_plan_from_yaml(plan_yaml)

    @pytest.mark.parametrize(
        ("literal", "expected_type"),
        [("null", "NoneType"), ("true", "bool"), ("42", "int"), ("{a: b}", "dict")],
        ids=["null", "bool", "number", "mapping"],
    )
    def test_yaml_plan_rejects_non_string_attachment_items(
        self, tmp_path: Path, literal: str, expected_type: str
    ) -> None:
        """Every item becomes a filesystem path at spawn, so a non-string one
        has to fail at load. `str()` turned these into '42' and "{'a': 'b'}" --
        paths that cannot exist, reported later by whatever tried to open them
        rather than by the loader that read the plan line."""
        img = _make_image(tmp_path / "img.png")
        plan_yaml = tmp_path / "plan.yaml"
        plan_yaml.write_text(
            "name: test\n"
            "stages:\n"
            "  - name: phase1\n"
            "    steps:\n"
            "      - title: With attachment\n"
            "        role: backend\n"
            f"        attachments: ['{img}', {literal}]\n"
        )
        from bernstein.core.planning.plan_loader import PlanLoadError, load_plan_from_yaml

        with pytest.raises(PlanLoadError) as exc_info:
            load_plan_from_yaml(plan_yaml)

        message = str(exc_info.value)
        assert "attachments[1]" in message
        assert expected_type in message
        # The plan line is what the reader has to edit, so the message names it.
        assert message.startswith("Step 0 in stage 'phase1':"), message

    @pytest.mark.parametrize("literal", ["null", ""], ids=["explicit-null", "absent"])
    def test_yaml_plan_absent_and_null_attachments_still_load(self, tmp_path: Path, literal: str) -> None:
        """Steps without attachments are the overwhelming majority; a strict
        rewrite that rejects the absent case would break every existing plan."""
        plan_yaml = tmp_path / "plan.yaml"
        field = f"        attachments: {literal}\n" if literal else ""
        plan_yaml.write_text(
            "name: test\n"
            "stages:\n"
            "  - name: phase1\n"
            "    steps:\n"
            "      - title: No attachment\n"
            "        role: backend\n" + field
        )
        from bernstein.core.planning.plan_loader import load_plan_from_yaml

        tasks = load_plan_from_yaml(plan_yaml)
        assert tasks[0].attachments == []


# ---------------------------------------------------------------------------
# Hash-matches-base64 invariant (bot-ack: 3284182756)
# ---------------------------------------------------------------------------


class TestHashMatchesBase64:
    def test_digest_matches_decoded_base64_not_disk(self, tmp_path: Path) -> None:
        """If the file changes after encode, the digest still matches what we sent."""
        import base64 as _b64
        import hashlib as _h

        original_payload = PNG_MAGIC + b"original-bytes"
        img = _make_image(tmp_path / "shot.png", payload=original_payload)
        chain = _audit_chain(tmp_path)
        cas = CASStore(tmp_path / "cas")

        result = build_attachment_context(
            attachments=[str(img)],
            worker_id="wkr-1",
            turn_seq=0,
            worktree_id="wt-a",
            cas=cas,
            audit_chain=chain,
        )
        digest = result.resolutions[0].sha256

        # Simulate file changing on disk after encode.
        img.write_bytes(b"different-bytes-on-disk")

        # The recorded digest equals SHA-256 of the bytes the adapter
        # inlines (the base64 payload), not the new on-disk bytes.
        b64 = result.context.inputs[0].content_base64 or ""
        adapter_bytes = _b64.b64decode(b64)
        assert digest == _h.sha256(adapter_bytes).hexdigest()
        assert adapter_bytes == original_payload


# ---------------------------------------------------------------------------
# Cross-worktree collision (bot-ack: 3284182761)
# ---------------------------------------------------------------------------


class TestCrossWorktreeCollision:
    def test_dual_attach_same_bytes_resolves_per_worktree(self, tmp_path: Path) -> None:
        """Same bytes attached in wt-a and wt-b both resolve in their worktree."""
        payload = PNG_MAGIC + b"shared-bytes"
        img_a = _make_image(tmp_path / "a.png", payload=payload)
        img_b = _make_image(tmp_path / "b.png", payload=payload)
        chain = _audit_chain(tmp_path)
        cas = CASStore(tmp_path / "cas")

        ra = build_attachment_context(
            attachments=[str(img_a)],
            worker_id="wkr-a",
            turn_seq=0,
            worktree_id="wt-a",
            cas=cas,
            audit_chain=chain,
        )
        rb = build_attachment_context(
            attachments=[str(img_b)],
            worker_id="wkr-b",
            turn_seq=0,
            worktree_id="wt-b",
            cas=cas,
            audit_chain=chain,
        )
        assert ra.resolutions[0].sha256 == rb.resolutions[0].sha256
        digest = ra.resolutions[0].sha256

        # wt-a can resolve.
        assert (
            resolve_attachment_for_worker(
                sha256=digest,
                requesting_worktree_id="wt-a",
                cas=cas,
                audit_chain=chain,
            )
            == payload
        )
        # wt-b can ALSO resolve (it has its own attach event).
        assert (
            resolve_attachment_for_worker(
                sha256=digest,
                requesting_worktree_id="wt-b",
                cas=cas,
                audit_chain=chain,
            )
            == payload
        )
        # wt-c cannot.
        with pytest.raises(WorktreeAccessDenied):
            resolve_attachment_for_worker(
                sha256=digest,
                requesting_worktree_id="wt-c",
                cas=cas,
                audit_chain=chain,
            )


# ---------------------------------------------------------------------------
# Non-hex digest rejection (bot-ack: 3284182781)
# ---------------------------------------------------------------------------


class TestParentUriRejectsNonHex:
    def test_rejects_non_hex_chars(self) -> None:
        from bernstein.core.persistence.lineage_signer import (
            LineageSignerError,
            build_attachment_parent_uri,
        )

        with pytest.raises(LineageSignerError, match="hex"):
            build_attachment_parent_uri("x" * 64)

    def test_rejects_uppercase(self) -> None:
        from bernstein.core.persistence.lineage_signer import (
            LineageSignerError,
            build_attachment_parent_uri,
        )

        with pytest.raises(LineageSignerError, match="hex"):
            build_attachment_parent_uri("A" * 64)

    def test_accepts_valid_hex(self) -> None:
        from bernstein.core.persistence.lineage_signer import build_attachment_parent_uri

        uri = build_attachment_parent_uri("0" * 64)
        assert uri.startswith("multimodal-attachment://")


# ---------------------------------------------------------------------------
# Atomic prev_chain_digest (bot-ack: 3284182792)
# ---------------------------------------------------------------------------


class TestAtomicPrevDigest:
    def test_concurrent_log_with_prev_digest_remains_linear(self, tmp_path: Path) -> None:
        """Two threads logging concurrently must not embed the same predecessor."""
        import threading

        chain = _audit_chain(tmp_path)
        N = 16

        def _writer(i: int) -> None:
            record_multimodal_attach(
                chain=chain,
                sha256=f"{i:064d}",
                mime="image/png",
                operator_install_id_sig="sig",
                worker_id=f"w-{i}",
                turn_seq=i,
                worktree_id="wt-a",
            )

        threads = [threading.Thread(target=_writer, args=(i,)) for i in range(N)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # The chain must remain verifiable after concurrent appends.
        valid, errors = chain.verify()
        assert valid, f"chain broken under concurrency: {errors}"

        # All N events landed.
        entries = chain.query(event_type=EVENT_MULTIMODAL_ATTACH)
        assert len(entries) == N

        # No two events share the same prev_chain_digest (linearity).
        prevs = [e.details["prev_chain_digest"] for e in entries]
        assert len(set(prevs)) == N


# ---------------------------------------------------------------------------
# Task.from_dict rejects scalar attachments (bot-ack: 3284182800)
# ---------------------------------------------------------------------------


class TestTaskFromDictAttachmentsType:
    def test_string_payload_rejected(self) -> None:
        raw = {
            "id": "t-1",
            "title": "x",
            "description": "y",
            "role": "backend",
            "attachments": "diagram.png",  # malformed scalar
        }
        with pytest.raises(TypeError, match="list of paths"):
            Task.from_dict(raw)

    def test_list_payload_accepted(self) -> None:
        raw = {
            "id": "t-1",
            "title": "x",
            "description": "y",
            "role": "backend",
            "attachments": ["a.png", "b.png"],
        }
        task = Task.from_dict(raw)
        assert task.attachments == ["a.png", "b.png"]
