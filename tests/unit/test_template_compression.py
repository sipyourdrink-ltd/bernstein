"""Tests for role-template compression (issue #2249).

Covers:
- Out-of-tree backup store: content-hash keying, readback verification,
  corruption detection.
- Compression engine: validated rewrite applied, failing rewrite leaves
  the template untouched, sensitive-gate refusal, no-savings skip.
- Restore (AC2): byte-identical reversal, verified by hash; refusal to
  clobber post-compression manual edits.
- Receipt (AC3): verifies in the audit chain and pins pre/post hashes.
- templates.lock round trip and drift recognition
  (compression_explains_digest_change).
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from bernstein.core.security.audit_chain import (
    EVENT_TEMPLATE_COMPRESSION_RECEIPT,
    AuditChainStore,
)
from bernstein.core.teams.drift import role_template_digest
from bernstein.core.tokens.template_compression import (
    BackupIntegrityError,
    CompressedFileRecord,
    CompressionLockEntry,
    TemplateCompressionError,
    TemplatesLockState,
    append_compression_entry,
    compress_role_templates,
    compression_explains_digest_change,
    load_compression_receipts,
    read_backup,
    read_templates_lock,
    receipt_from_details,
    restore_role_templates,
    store_backup,
    templates_lock_path,
    verify_compression_receipts,
    write_templates_lock,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

ORIGINAL_SYSTEM = """\
# You are a Backend Engineer

## Your specialization
You implement server-side logic and APIs with great care and attention,
always reading the existing code before writing anything new at all.

## Rules
- Run `uv run ruff check src/` before completing
- Style guide: https://docs.example.test/style

## Current task
{{TASK_DESCRIPTION}}
"""

COMPRESSED_SYSTEM = """\
# You are a Backend Engineer

## Your specialization
Server-side logic and APIs; read existing code first.

## Rules
- Run `uv run ruff check src/` before completing
- Style guide: https://docs.example.test/style

## Current task
{{TASK_DESCRIPTION}}
"""


def _write_role(workdir: Path, role: str = "backend", text: str = ORIGINAL_SYSTEM) -> Path:
    role_dir = workdir / "templates" / "roles" / role
    role_dir.mkdir(parents=True, exist_ok=True)
    (role_dir / "system_prompt.md").write_text(text, encoding="utf-8")
    return role_dir


def _chain(tmp_path: Path) -> AuditChainStore:
    return AuditChainStore(tmp_path / "audit", key=b"k" * 32)


def _compress(
    workdir: Path,
    tmp_path: Path,
    *,
    llm_response: str = COMPRESSED_SYSTEM,
    chain: AuditChainStore | None = None,
):
    return compress_role_templates(
        "backend",
        workdir=workdir,
        llm_call=lambda prompt: llm_response,
        adapter="openrouter",
        model="test-model",
        chain=chain,
        backup_root=tmp_path / "backups",
    )


# ---------------------------------------------------------------------------
# Backup store
# ---------------------------------------------------------------------------


class TestBackupStore:
    def test_store_is_keyed_by_content_hash(self, tmp_path: Path) -> None:
        digest = store_backup(b"original bytes", backup_root=tmp_path / "b")
        assert digest == hashlib.sha256(b"original bytes").hexdigest()
        assert (tmp_path / "b" / digest).read_bytes() == b"original bytes"

    def test_store_is_idempotent(self, tmp_path: Path) -> None:
        first = store_backup(b"same", backup_root=tmp_path / "b")
        second = store_backup(b"same", backup_root=tmp_path / "b")
        assert first == second

    def test_read_back_verifies_hash(self, tmp_path: Path) -> None:
        digest = store_backup(b"payload", backup_root=tmp_path / "b")
        assert read_backup(digest, backup_root=tmp_path / "b") == b"payload"

    def test_corrupted_backup_is_detected(self, tmp_path: Path) -> None:
        digest = store_backup(b"payload", backup_root=tmp_path / "b")
        (tmp_path / "b" / digest).write_bytes(b"tampered")
        with pytest.raises(BackupIntegrityError, match="hash verification"):
            read_backup(digest, backup_root=tmp_path / "b")

    def test_missing_backup_raises(self, tmp_path: Path) -> None:
        with pytest.raises(BackupIntegrityError, match="not found"):
            read_backup("0" * 64, backup_root=tmp_path / "b")


# ---------------------------------------------------------------------------
# Compression engine
# ---------------------------------------------------------------------------


class TestCompressRole:
    def test_validated_rewrite_is_applied(self, tmp_path: Path) -> None:
        role_dir = _write_role(tmp_path)
        outcome = _compress(tmp_path, tmp_path)
        assert outcome.applied, outcome.reason
        assert (role_dir / "system_prompt.md").read_text(encoding="utf-8") == COMPRESSED_SYSTEM
        assert outcome.post_tokens < outcome.pre_tokens

    def test_receipt_shape(self, tmp_path: Path) -> None:
        role_dir = _write_role(tmp_path)
        pre_digest = role_template_digest(role_dir)
        outcome = _compress(tmp_path, tmp_path)
        receipt = outcome.receipt
        assert receipt is not None
        assert receipt.role == "backend"
        assert receipt.pre_sha256 == pre_digest
        assert receipt.post_sha256 == role_template_digest(role_dir)
        assert receipt.adapter == "openrouter"
        assert receipt.model == "test-model"
        assert receipt.pre_tokens > receipt.post_tokens > 0
        assert all(passed for _, passed in receipt.validators)
        assert receipt.files[0].path == "system_prompt.md"
        assert receipt.files[0].pre_sha256 == hashlib.sha256(ORIGINAL_SYSTEM.encode()).hexdigest()
        assert receipt.files[0].post_sha256 == hashlib.sha256(COMPRESSED_SYSTEM.encode()).hexdigest()

    def test_invalid_rewrite_leaves_template_untouched(self, tmp_path: Path) -> None:
        role_dir = _write_role(tmp_path)
        bad = COMPRESSED_SYSTEM.replace("https://docs.example.test/style", "")
        outcome = _compress(tmp_path, tmp_path, llm_response=bad)
        assert not outcome.applied
        assert "urls" in outcome.reason
        assert (role_dir / "system_prompt.md").read_text(encoding="utf-8") == ORIGINAL_SYSTEM
        assert not templates_lock_path(tmp_path).exists()

    def test_no_savings_leaves_template_untouched(self, tmp_path: Path) -> None:
        role_dir = _write_role(tmp_path)
        outcome = _compress(tmp_path, tmp_path, llm_response=ORIGINAL_SYSTEM)
        assert not outcome.applied
        assert "saved no tokens" in outcome.reason
        assert (role_dir / "system_prompt.md").read_text(encoding="utf-8") == ORIGINAL_SYSTEM

    def test_sensitive_gate_refuses_credential_shaped_template(self, tmp_path: Path) -> None:
        secret = "AKIA" + "A" * 16
        text = ORIGINAL_SYSTEM + f"\nExample key: {secret}\n"
        role_dir = _write_role(tmp_path, text=text)
        calls = 0

        def llm_call(prompt: str) -> str:
            nonlocal calls
            calls += 1
            return COMPRESSED_SYSTEM

        outcome = compress_role_templates(
            "backend",
            workdir=tmp_path,
            llm_call=llm_call,
            adapter="openrouter",
            model="test-model",
            backup_root=tmp_path / "backups",
        )
        assert not outcome.applied
        assert "sensitive gate" in outcome.reason
        # Nothing was sent to the model and nothing was written.
        assert calls == 0
        assert (role_dir / "system_prompt.md").read_text(encoding="utf-8") == text

    def test_unknown_role_raises(self, tmp_path: Path) -> None:
        _write_role(tmp_path)
        with pytest.raises(TemplateCompressionError, match="not found"):
            compress_role_templates(
                "no-such-role",
                workdir=tmp_path,
                llm_call=lambda prompt: "",
                adapter="a",
                model="m",
                backup_root=tmp_path / "backups",
            )

    def test_bundled_templates_are_refused(self, tmp_path: Path) -> None:
        # No project-local templates/ dir: resolution falls back to the
        # bundled package copy, which compression must never rewrite.
        with pytest.raises(TemplateCompressionError, match="project-local"):
            compress_role_templates(
                "backend",
                workdir=tmp_path,
                llm_call=lambda prompt: "",
                adapter="a",
                model="m",
                backup_root=tmp_path / "backups",
            )

    def test_include_role_names_are_refused(self, tmp_path: Path) -> None:
        _write_role(tmp_path)
        with pytest.raises(TemplateCompressionError, match="not a compressible"):
            compress_role_templates(
                "_includes",
                workdir=tmp_path,
                llm_call=lambda prompt: "",
                adapter="a",
                model="m",
                backup_root=tmp_path / "backups",
            )

    def test_lock_entry_written_with_digest_edge(self, tmp_path: Path) -> None:
        role_dir = _write_role(tmp_path)
        pre_digest = role_template_digest(role_dir)
        outcome = _compress(tmp_path, tmp_path)
        state = read_templates_lock(templates_lock_path(tmp_path))
        assert len(state.compressions) == 1
        row = state.compressions[0]
        assert row.role == "backend"
        assert row.pre_digest == pre_digest
        assert row.post_digest == role_template_digest(role_dir)
        assert outcome.receipt is not None
        assert row.correlation_id == outcome.receipt.correlation_id


# ---------------------------------------------------------------------------
# Restore (AC2)
# ---------------------------------------------------------------------------


class TestRestore:
    def test_restore_is_byte_identical_and_hash_verified(self, tmp_path: Path) -> None:
        role_dir = _write_role(tmp_path)
        original_bytes = (role_dir / "system_prompt.md").read_bytes()
        pre_digest = role_template_digest(role_dir)

        assert _compress(tmp_path, tmp_path).applied
        assert (role_dir / "system_prompt.md").read_bytes() != original_bytes

        outcome = restore_role_templates(
            "backend",
            workdir=tmp_path,
            backup_root=tmp_path / "backups",
        )
        assert (role_dir / "system_prompt.md").read_bytes() == original_bytes
        assert outcome.pre_digest == pre_digest
        assert outcome.restored_files == ("system_prompt.md",)
        # The reversed lock row is gone; a second restore has nothing to do.
        with pytest.raises(TemplateCompressionError, match="no receipted compression"):
            restore_role_templates("backend", workdir=tmp_path, backup_root=tmp_path / "backups")

    def test_restore_refuses_over_manual_edits(self, tmp_path: Path) -> None:
        role_dir = _write_role(tmp_path)
        assert _compress(tmp_path, tmp_path).applied
        prompt = role_dir / "system_prompt.md"
        prompt.write_text(prompt.read_text(encoding="utf-8") + "x", encoding="utf-8")
        with pytest.raises(TemplateCompressionError, match="modified after compression"):
            restore_role_templates("backend", workdir=tmp_path, backup_root=tmp_path / "backups")

    def test_restore_refuses_when_untouched_files_changed(self, tmp_path: Path) -> None:
        role_dir = _write_role(tmp_path)
        (role_dir / "config.yaml").write_text("default_model: sonnet\n", encoding="utf-8")
        assert _compress(tmp_path, tmp_path).applied
        # A file the compression never rewrote changes afterwards; the
        # directory is no longer in the recorded post-compression state.
        (role_dir / "config.yaml").write_text("default_model: opus\n", encoding="utf-8")
        with pytest.raises(TemplateCompressionError, match="directory changed since compression"):
            restore_role_templates("backend", workdir=tmp_path, backup_root=tmp_path / "backups")

    def test_restore_without_compression_raises(self, tmp_path: Path) -> None:
        _write_role(tmp_path)
        with pytest.raises(TemplateCompressionError, match="no receipted compression"):
            restore_role_templates("backend", workdir=tmp_path, backup_root=tmp_path / "backups")


# ---------------------------------------------------------------------------
# Receipt in the audit chain (AC3)
# ---------------------------------------------------------------------------


class TestChainReceipt:
    def test_receipt_verifies_in_chain_and_pins_hashes(self, tmp_path: Path) -> None:
        role_dir = _write_role(tmp_path)
        pre_digest = role_template_digest(role_dir)
        chain = _chain(tmp_path)

        outcome = _compress(tmp_path, tmp_path, chain=chain)
        assert outcome.applied

        ok, errors = chain.verify()
        assert ok, errors

        receipts = load_compression_receipts(chain, role="backend")
        assert len(receipts) == 1
        receipt = receipts[0]
        assert receipt.pre_sha256 == pre_digest
        assert receipt.post_sha256 == role_template_digest(role_dir)

        # The previous chain digest is embedded in the payload.
        events = chain.query(event_type=EVENT_TEMPLATE_COMPRESSION_RECEIPT)
        assert len(events) == 1
        assert "prev_chain_digest" in events[0].details

        lock_state = read_templates_lock(templates_lock_path(tmp_path))
        ok, errors = verify_compression_receipts(chain, lock_state=lock_state)
        assert ok, errors

    def test_lock_row_without_chain_receipt_fails_verification(self, tmp_path: Path) -> None:
        chain = _chain(tmp_path)
        lock_state = TemplatesLockState(
            compressions=[
                CompressionLockEntry(
                    role="backend",
                    correlation_id="tmplc-missing",
                    pre_digest="a" * 64,
                    post_digest="b" * 64,
                    adapter="openrouter",
                    model="m",
                    chain_head="",
                    timestamp="2026-01-01T00:00:00+00:00",
                )
            ]
        )
        ok, errors = verify_compression_receipts(chain, lock_state=lock_state)
        assert not ok
        assert any("no chain receipt" in err for err in errors)

    def test_digest_mismatch_between_lock_and_receipt_fails(self, tmp_path: Path) -> None:
        _write_role(tmp_path)
        chain = _chain(tmp_path)
        outcome = _compress(tmp_path, tmp_path, chain=chain)
        assert outcome.receipt is not None
        tampered = TemplatesLockState(
            compressions=[
                CompressionLockEntry(
                    role="backend",
                    correlation_id=outcome.receipt.correlation_id,
                    pre_digest="c" * 64,
                    post_digest="d" * 64,
                    adapter="openrouter",
                    model="m",
                    chain_head="",
                    timestamp="2026-01-01T00:00:00+00:00",
                )
            ]
        )
        ok, errors = verify_compression_receipts(chain, lock_state=tampered)
        assert not ok
        assert any("disagree" in err for err in errors)

    def test_receipt_round_trips_through_details(self, tmp_path: Path) -> None:
        _write_role(tmp_path)
        chain = _chain(tmp_path)
        outcome = _compress(tmp_path, tmp_path, chain=chain)
        assert outcome.receipt is not None
        rebuilt = receipt_from_details(outcome.receipt.to_details())
        assert rebuilt == outcome.receipt


# ---------------------------------------------------------------------------
# templates.lock round trip + drift recognition edges
# ---------------------------------------------------------------------------


def _entry(role: str, pre: str, post: str, correlation: str = "tmplc-1") -> CompressionLockEntry:
    return CompressionLockEntry(
        role=role,
        correlation_id=correlation,
        pre_digest=pre,
        post_digest=post,
        adapter="openrouter",
        model="m",
        chain_head="h" * 64,
        timestamp="2026-01-01T00:00:00+00:00",
        files=(CompressedFileRecord(path="system_prompt.md", pre_sha256="e" * 64, post_sha256="f" * 64),),
    )


class TestTemplatesLock:
    def test_write_read_round_trip(self, tmp_path: Path) -> None:
        lock = tmp_path / "templates.lock"
        state = TemplatesLockState(compressions=[_entry("backend", "a" * 64, "b" * 64)])
        write_templates_lock(lock, state)
        loaded = read_templates_lock(lock)
        assert loaded.compressions == state.compressions

    def test_append_preserves_existing_rows(self, tmp_path: Path) -> None:
        lock = tmp_path / "templates.lock"
        append_compression_entry(lock, _entry("backend", "a" * 64, "b" * 64, "tmplc-1"))
        append_compression_entry(lock, _entry("qa", "c" * 64, "d" * 64, "tmplc-2"))
        loaded = read_templates_lock(lock)
        assert [row.role for row in loaded.compressions] == ["backend", "qa"]

    def test_unparseable_lock_returns_empty_state(self, tmp_path: Path) -> None:
        lock = tmp_path / "templates.lock"
        lock.write_text("not [ valid toml", encoding="utf-8")
        assert read_templates_lock(lock).compressions == []


class TestCompressionExplainsDigestChange:
    def test_direct_edge_is_recognized(self) -> None:
        state = TemplatesLockState(compressions=[_entry("backend", "a" * 64, "b" * 64)])
        assert compression_explains_digest_change(state, role="backend", pinned_digest="a" * 64, actual_digest="b" * 64)

    def test_chained_recompression_is_recognized(self) -> None:
        state = TemplatesLockState(
            compressions=[
                _entry("backend", "a" * 64, "b" * 64, "tmplc-1"),
                _entry("backend", "b" * 64, "c" * 64, "tmplc-2"),
            ]
        )
        assert compression_explains_digest_change(state, role="backend", pinned_digest="a" * 64, actual_digest="c" * 64)

    def test_unrelated_edit_is_not_recognized(self) -> None:
        state = TemplatesLockState(compressions=[_entry("backend", "a" * 64, "b" * 64)])
        assert not compression_explains_digest_change(
            state, role="backend", pinned_digest="a" * 64, actual_digest="9" * 64
        )

    def test_other_roles_edges_do_not_apply(self) -> None:
        state = TemplatesLockState(compressions=[_entry("qa", "a" * 64, "b" * 64)])
        assert not compression_explains_digest_change(
            state, role="backend", pinned_digest="a" * 64, actual_digest="b" * 64
        )

    def test_missing_template_marker_is_never_intentional(self) -> None:
        state = TemplatesLockState(compressions=[_entry("backend", "a" * 64, "b" * 64)])
        assert not compression_explains_digest_change(
            state, role="backend", pinned_digest="a" * 64, actual_digest="<missing>"
        )
