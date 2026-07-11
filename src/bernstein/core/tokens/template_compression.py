"""Operator-gated role-template compression with chained receipts (issue #2249).

Every worker spawn renders its role's ``system_prompt.md`` and
``task_prompt.md``; that fixed input cost recurs on every spawn and
again after every compaction reinjection. This module implements the
one-time compression pass behind ``bernstein templates compress`` - it
is never automatic:

* **LLM rewrite, mechanically gated.** The rewrite candidate must pass
  every validator in :mod:`bernstein.core.tokens.
  template_compress_validate` (base compaction validators plus the
  template-specific ones), with at most two targeted fix passes. A
  failing candidate never reaches disk.
* **Sensitive gate before the model.** Template text is scanned by the
  compaction sensitive gate before anything is sent to the configured
  adapter; any finding (redaction or refusal) refuses the compression
  outright, because writing redaction placeholders back into a template
  would corrupt it.
* **Out-of-tree backups, readback-verified.** Originals are stored
  under ``~/.local/share/bernstein/template-backups/`` keyed by content
  hash, so template loaders never re-ingest them, and each write is
  read back and re-hashed before the working copy is touched.
  ``bernstein templates restore <role>`` reverses the compression
  byte-identically, verified by hash.
* **Chained receipt.** ``{role, pre_sha256, post_sha256, pre_tokens,
  post_tokens, validators, adapter, model}`` is appended to the HMAC
  audit chain as a ``template.compression.receipt`` event with the
  previous chain digest embedded in the payload. The receipt's ``ts``
  is the boundary for before/after ``bernstein cost --by role`` ledger
  windows; savings figures come from the ledger, never from this
  module.
* **Lockfile entry for drift.** Each applied compression appends a row
  to ``templates.lock`` mapping the role's pre and post directory
  digests, so the team-manifest drift check (#2248) recognizes a
  receipted compression as intentional rather than reporting spurious
  drift.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import time
import tomllib
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, cast

from bernstein.core.skills.catalog.lockfile import (
    _acquire_lock,  # pyright: ignore[reportPrivateUsage]
    _release_lock,  # pyright: ignore[reportPrivateUsage]
)
from bernstein.core.skills.lifecycle import _toml_quote  # pyright: ignore[reportPrivateUsage]
from bernstein.core.tokens.template_compress_validate import (
    split_frontmatter,
    validate_template_rewrite,
)
from bernstein.core.tokens.token_estimation import estimate_tokens_for_text

if TYPE_CHECKING:
    from collections.abc import Callable

    from bernstein.core.security.audit import AuditEvent
    from bernstein.core.security.audit_chain import AuditChainStore

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Filename of the template compression lock; sibling of ``teams.lock``
#: at the project root.
TEMPLATES_LOCK_FILENAME: Final[str] = "templates.lock"

#: Prompt files inside a role template directory that compression may
#: rewrite. Everything else (``config.yaml``, includes) is never touched.
TEMPLATE_PROMPT_FILENAMES: Final[tuple[str, ...]] = ("system_prompt.md", "task_prompt.md")

#: Actor recorded on chain events emitted by this module.
AUDIT_ACTOR: Final[str] = "bernstein.templates"

#: Resource type recorded on chain events emitted by this module.
AUDIT_RESOURCE_TYPE: Final[str] = "role_template"

#: ``assumed_type`` used for deterministic template token estimates.
_TOKEN_ESTIMATE_TYPE: Final[str] = "text"

_HEX_DIGEST_RE: Final[re.Pattern[str]] = re.compile(r"^[0-9a-f]{64}$")


class TemplateCompressionError(Exception):
    """Base error for template compression and restore operations."""


class BackupIntegrityError(TemplateCompressionError):
    """A backup readback or hash verification failed."""


# ---------------------------------------------------------------------------
# Out-of-tree backup store (content-hash keyed, readback-verified)
# ---------------------------------------------------------------------------


def default_backup_root() -> Path:
    """Return the out-of-tree backup directory for template originals.

    XDG-conventional: ``$XDG_DATA_HOME/bernstein/template-backups`` with
    the documented ``~/.local/share`` fallback. Out-of-tree by design so
    template loaders (which scan the project tree) never re-ingest the
    uncompressed originals.
    """
    raw = os.environ.get("XDG_DATA_HOME", "").strip()
    base = Path(raw) if raw else Path.home() / ".local" / "share"
    return base / "bernstein" / "template-backups"


def store_backup(content: bytes, *, backup_root: Path | None = None) -> str:
    """Store *content* keyed by its SHA-256, verifying the write by readback.

    Idempotent: storing the same bytes twice reuses the existing file
    (after re-verifying it).

    Args:
        content: The exact original file bytes.
        backup_root: Backup directory override (tests); defaults to
            :func:`default_backup_root`.

    Returns:
        The lower-case SHA-256 hex digest keying the backup.

    Raises:
        BackupIntegrityError: When the readback bytes do not hash to the
            expected digest.
    """
    root = backup_root if backup_root is not None else default_backup_root()
    digest = hashlib.sha256(content).hexdigest()
    root.mkdir(parents=True, exist_ok=True)
    target = root / digest
    if not target.is_file():
        tmp = target.with_suffix(".tmp")
        tmp.write_bytes(content)
        tmp.replace(target)
    readback = target.read_bytes()
    if hashlib.sha256(readback).hexdigest() != digest:
        raise BackupIntegrityError(f"backup readback verification failed for {target}")
    return digest


def read_backup(digest: str, *, backup_root: Path | None = None) -> bytes:
    """Return the backup bytes for *digest*, hash-verified.

    Args:
        digest: Lower-case SHA-256 hex digest of the original content.
        backup_root: Backup directory override (tests).

    Returns:
        The original bytes.

    Raises:
        BackupIntegrityError: When the backup is missing or its bytes no
            longer hash to *digest*.
    """
    root = backup_root if backup_root is not None else default_backup_root()
    target = root / digest
    if not target.is_file():
        raise BackupIntegrityError(f"backup not found for digest {digest}")
    content = target.read_bytes()
    if hashlib.sha256(content).hexdigest() != digest:
        raise BackupIntegrityError(f"backup content for digest {digest} failed hash verification")
    return content


# ---------------------------------------------------------------------------
# Receipt
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CompressedFileRecord:
    """Pre/post content hashes for one rewritten template file.

    Attributes:
        path: File path relative to the role template directory (POSIX).
        pre_sha256: SHA-256 of the original bytes (the backup key).
        post_sha256: SHA-256 of the compressed bytes on disk.
    """

    path: str
    pre_sha256: str
    post_sha256: str


@dataclass(frozen=True, slots=True)
class TemplateCompressionReceipt:
    """One role-template compression, in the shape the chain records it.

    Attributes:
        role: Role whose templates were compressed.
        pre_sha256: Role template directory digest before compression
            (:func:`bernstein.core.teams.drift.role_template_digest`).
        post_sha256: Role template directory digest after compression.
        pre_tokens: Deterministic token estimate before compression
            (sum over the rewritten files).
        post_tokens: Deterministic token estimate after compression.
        validators: ``(name, passed)`` pairs in validator run order.
        adapter: Configured adapter/provider used for the rewrite.
        model: Model name used for the rewrite.
        retry_count: Targeted fix passes executed before validation.
        files: Per-file pre/post content hashes.
        ts: Unix epoch seconds when the receipt was built. This is the
            boundary for before/after ``bernstein cost --by role``
            ledger windows; savings are claimed only from the ledger.
        correlation_id: Id shared by the chain event and the
            ``templates.lock`` row.
    """

    role: str
    pre_sha256: str
    post_sha256: str
    pre_tokens: int
    post_tokens: int
    validators: tuple[tuple[str, bool], ...]
    adapter: str
    model: str
    retry_count: int
    files: tuple[CompressedFileRecord, ...]
    ts: float
    correlation_id: str

    def to_details(self) -> dict[str, Any]:
        """Return the JSON-safe payload recorded in the audit chain."""
        return {
            "role": self.role,
            "pre_sha256": self.pre_sha256,
            "post_sha256": self.post_sha256,
            "pre_tokens": self.pre_tokens,
            "post_tokens": self.post_tokens,
            "validators": [{"name": name, "result": "pass" if passed else "fail"} for name, passed in self.validators],
            "adapter": self.adapter,
            "model": self.model,
            "retry_count": self.retry_count,
            "files": [
                {"path": record.path, "pre_sha256": record.pre_sha256, "post_sha256": record.post_sha256}
                for record in self.files
            ],
            "ts": self.ts,
            "correlation_id": self.correlation_id,
        }


def receipt_from_details(details: dict[str, Any]) -> TemplateCompressionReceipt:
    """Rebuild a receipt from a chain event's details payload."""
    raw_validators = details.get("validators") or []
    raw_files = details.get("files") or []
    return TemplateCompressionReceipt(
        role=str(details.get("role", "")),
        pre_sha256=str(details.get("pre_sha256", "")),
        post_sha256=str(details.get("post_sha256", "")),
        pre_tokens=int(details.get("pre_tokens", 0)),
        post_tokens=int(details.get("post_tokens", 0)),
        validators=tuple((str(v.get("name", "")), v.get("result") == "pass") for v in raw_validators),
        adapter=str(details.get("adapter", "")),
        model=str(details.get("model", "")),
        retry_count=int(details.get("retry_count", 0)),
        files=tuple(
            CompressedFileRecord(
                path=str(f.get("path", "")),
                pre_sha256=str(f.get("pre_sha256", "")),
                post_sha256=str(f.get("post_sha256", "")),
            )
            for f in raw_files
        ),
        ts=float(details.get("ts", 0.0)),
        correlation_id=str(details.get("correlation_id", "")),
    )


def record_compression_receipt(*, chain: AuditChainStore, receipt: TemplateCompressionReceipt) -> AuditEvent:
    """Append a ``template.compression.receipt`` event into *chain*.

    Same anchoring as compaction receipts (#2246): the previous chain
    digest is embedded in the payload before the HMAC is computed, so
    the receipt is position-locked in the chain.
    """
    from bernstein.core.security.audit_chain import EVENT_TEMPLATE_COMPRESSION_RECEIPT

    return chain.log_with_prev_digest(
        event_type=EVENT_TEMPLATE_COMPRESSION_RECEIPT,
        actor=AUDIT_ACTOR,
        resource_type=AUDIT_RESOURCE_TYPE,
        resource_id=receipt.role,
        details=receipt.to_details(),
    )


def load_compression_receipts(chain: AuditChainStore, *, role: str | None = None) -> list[TemplateCompressionReceipt]:
    """Return compression receipts recorded in *chain*, in chain order.

    Malformed receipt events are skipped with a warning;
    :func:`verify_compression_receipts` turns them into hard errors.
    """
    from bernstein.core.security.audit_chain import EVENT_TEMPLATE_COMPRESSION_RECEIPT

    receipts: list[TemplateCompressionReceipt] = []
    for event in chain.query(event_type=EVENT_TEMPLATE_COMPRESSION_RECEIPT):
        try:
            receipt = receipt_from_details(event.details)
        except (ValueError, TypeError) as exc:
            logger.warning("Skipping malformed template compression receipt event: %s", exc)
            continue
        if role is not None and receipt.role != role:
            continue
        receipts.append(receipt)
    return receipts


def verify_compression_receipts(
    chain: AuditChainStore,
    *,
    lock_state: TemplatesLockState | None = None,
    role: str | None = None,
) -> tuple[bool, list[str]]:
    """Verify compression receipts against the chain and the lockfile.

    Three checks, all of which must hold:

    1. The audit chain's HMAC chain verifies end to end.
    2. Every ``template.compression.receipt`` event parses.
    3. When *lock_state* is given, every lock row has a chain receipt
       with the same correlation id pinning the same pre/post directory
       digests.

    Returns:
        ``(ok, errors)``.
    """
    from bernstein.core.security.audit_chain import EVENT_TEMPLATE_COMPRESSION_RECEIPT

    errors: list[str] = []
    chain_ok, chain_errors = chain.verify()
    if not chain_ok:
        errors.extend(f"audit chain: {err}" for err in chain_errors)

    receipts: dict[str, TemplateCompressionReceipt] = {}
    for event in chain.query(event_type=EVENT_TEMPLATE_COMPRESSION_RECEIPT):
        try:
            receipt = receipt_from_details(event.details)
        except (ValueError, TypeError) as exc:
            errors.append(f"audit chain: receipt event at {event.timestamp} unparseable: {exc}")
            continue
        if role is not None and receipt.role != role:
            continue
        receipts[receipt.correlation_id] = receipt

    if lock_state is not None:
        for row in lock_state.compressions:
            if role is not None and row.role != role:
                continue
            receipt = receipts.get(row.correlation_id)
            if receipt is None:
                errors.append(
                    f"templates.lock row for role {row.role!r} (correlation={row.correlation_id}) "
                    f"has no chain receipt; verification fails"
                )
                continue
            if receipt.pre_sha256 != row.pre_digest or receipt.post_sha256 != row.post_digest:
                errors.append(
                    f"templates.lock row for role {row.role!r} pins digests that disagree with "
                    f"chain receipt {row.correlation_id}"
                )
    return (not errors, errors)


# ---------------------------------------------------------------------------
# templates.lock (drift recognition + restore bookkeeping)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CompressionLockEntry:
    """One ``[[compressions]]`` row in ``templates.lock``.

    Attributes:
        role: Compressed role name.
        correlation_id: Matches the chain receipt's correlation id.
        pre_digest: Role template directory digest before compression.
        post_digest: Role template directory digest after compression.
        adapter: Adapter/provider used for the rewrite.
        model: Model used for the rewrite.
        chain_head: HMAC of the chain receipt event ("" when the chain
            was unavailable at compression time).
        timestamp: ISO-8601 UTC timestamp of the compression.
        files: Per-file pre/post content hashes (restore bookkeeping;
            ``pre_sha256`` is the backup key).
    """

    role: str
    correlation_id: str
    pre_digest: str
    post_digest: str
    adapter: str
    model: str
    chain_head: str
    timestamp: str
    files: tuple[CompressedFileRecord, ...] = ()


@dataclass(frozen=True)
class TemplatesLockState:
    """Parsed view of ``templates.lock``."""

    compressions: list[CompressionLockEntry] = field(default_factory=list)

    def entries_for(self, role: str) -> list[CompressionLockEntry]:
        """Return the rows for *role* in file order."""
        return [row for row in self.compressions if row.role == role]


def templates_lock_path(workdir: Path) -> Path:
    """Return the ``templates.lock`` path for *workdir* (project root)."""
    return workdir / TEMPLATES_LOCK_FILENAME


def read_templates_lock(lock_path: Path) -> TemplatesLockState:
    """Parse ``templates.lock``, returning an empty state on any parse error."""
    if not lock_path.is_file():
        return TemplatesLockState()
    try:
        data = tomllib.loads(lock_path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return TemplatesLockState()

    rows: list[CompressionLockEntry] = []
    for raw in cast("list[object]", data.get("compressions", [])):
        if not isinstance(raw, dict):
            continue
        row = cast("dict[str, object]", raw)
        files: list[CompressedFileRecord] = []
        for raw_file in cast("list[object]", row.get("files", [])):
            if not isinstance(raw_file, dict):
                continue
            file_row = cast("dict[str, object]", raw_file)
            path = file_row.get("path")
            pre = file_row.get("pre_sha256")
            post = file_row.get("post_sha256")
            if isinstance(path, str) and isinstance(pre, str) and isinstance(post, str):
                files.append(CompressedFileRecord(path=path, pre_sha256=pre, post_sha256=post))
        try:
            rows.append(
                CompressionLockEntry(
                    role=_required(row, "role"),
                    correlation_id=_required(row, "correlation_id"),
                    pre_digest=_required(row, "pre_digest"),
                    post_digest=_required(row, "post_digest"),
                    adapter=_required(row, "adapter"),
                    model=_required(row, "model"),
                    chain_head=str(row.get("chain_head", "")),
                    timestamp=_required(row, "timestamp"),
                    files=tuple(files),
                )
            )
        except KeyError:
            continue
    return TemplatesLockState(compressions=rows)


def _required(row: dict[str, object], key: str) -> str:
    value = row.get(key)
    if not isinstance(value, str) or not value:
        raise KeyError(key)
    return value


def write_templates_lock(lock_path: Path, state: TemplatesLockState) -> None:
    """Write ``templates.lock`` atomically with deterministic formatting."""
    lines: list[str] = [
        "# bernstein templates lock file - regenerated by `bernstein templates` commands.",
        "# Do not edit by hand.",
        "",
    ]
    for row in state.compressions:
        lines.extend(
            (
                "[[compressions]]",
                f"role = {_toml_quote(row.role)}",
                f"correlation_id = {_toml_quote(row.correlation_id)}",
                f"pre_digest = {_toml_quote(row.pre_digest)}",
                f"post_digest = {_toml_quote(row.post_digest)}",
                f"adapter = {_toml_quote(row.adapter)}",
                f"model = {_toml_quote(row.model)}",
                f"chain_head = {_toml_quote(row.chain_head)}",
                f"timestamp = {_toml_quote(row.timestamp)}",
            )
        )
        for record in row.files:
            lines.extend(
                (
                    "",
                    "[[compressions.files]]",
                    f"path = {_toml_quote(record.path)}",
                    f"pre_sha256 = {_toml_quote(record.pre_sha256)}",
                    f"post_sha256 = {_toml_quote(record.post_sha256)}",
                )
            )
        lines.append("")

    lock_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = lock_path.with_suffix(lock_path.suffix + ".tmp")
    tmp.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    tmp.replace(lock_path)


def append_compression_entry(lock_path: Path, entry: CompressionLockEntry) -> TemplatesLockState:
    """Append a compression row under the coarse cross-worktree file lock."""
    guard = _acquire_lock(lock_path)
    try:
        state = read_templates_lock(lock_path)
        new_state = TemplatesLockState(compressions=[*state.compressions, entry])
        write_templates_lock(lock_path, new_state)
        return new_state
    finally:
        _release_lock(guard)


def remove_compression_entry(lock_path: Path, correlation_id: str) -> TemplatesLockState:
    """Remove the row with *correlation_id* under the file lock."""
    guard = _acquire_lock(lock_path)
    try:
        state = read_templates_lock(lock_path)
        new_state = TemplatesLockState(
            compressions=[row for row in state.compressions if row.correlation_id != correlation_id]
        )
        write_templates_lock(lock_path, new_state)
        return new_state
    finally:
        _release_lock(guard)


# ---------------------------------------------------------------------------
# Compression engine
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CompressionOutcome:
    """Result of one role compression attempt.

    Attributes:
        role: The role that was processed.
        applied: Whether compressed templates were written to disk.
        reason: Human-readable explanation when ``applied`` is False.
        receipt: The chained receipt when ``applied`` is True.
        pre_tokens: Token estimate before (rewritten files only).
        post_tokens: Token estimate after.
    """

    role: str
    applied: bool
    reason: str = ""
    receipt: TemplateCompressionReceipt | None = None
    pre_tokens: int = 0
    post_tokens: int = 0


def build_compression_prompt(body: str) -> str:
    """Build the rewrite prompt for one template body (frontmatter removed).

    The prompt states every mechanical invariant the validators enforce,
    so a competent rewrite passes on the first attempt; the validators
    remain the gate regardless of what the model returns.
    """
    return (
        "Rewrite the role prompt template below to use fewer tokens while "
        "preserving every instruction. HARD CONSTRAINTS - the rewrite is "
        "mechanically rejected if any is violated:\n"
        "- Keep every heading line byte-identical and in the same order.\n"
        "- Keep every fenced code block byte-identical (or drop one whole, never edit it).\n"
        "- Keep every URL exactly as written; add none.\n"
        "- Keep every inline `code` span exactly as written; add none.\n"
        "- Keep every {{PLACEHOLDER}}, {{#IF ...}}/{{/IF}} marker, and {{INCLUDE ...}} "
        "directive byte-identical.\n"
        "- Copy any completion-contract instruction section verbatim; never reword it.\n"
        "Shorten only the surrounding prose. Return ONLY the rewritten template text, "
        "no commentary, no code fences around the whole answer.\n\n"
        f"<<<TEMPLATE\n{body}\nTEMPLATE>>>\n"
    )


def _extract_rewrite(raw: str) -> str:
    """Return the rewritten template from a raw model response.

    Strips the delimiters some models echo back; otherwise returns the
    response unchanged (the validators are the real gate).
    """
    text = raw.strip("\n")
    if text.startswith("<<<TEMPLATE\n") and text.endswith("\nTEMPLATE>>>"):
        text = text[len("<<<TEMPLATE\n") : -len("\nTEMPLATE>>>")]
    return text


def _gate_refuses(text: str, *, role: str, chain: AuditChainStore | None) -> bool:
    """Run the sensitive gate over *text*; True when compression must stop.

    Unlike compaction (which forwards redacted text), template
    compression refuses on ANY finding: writing redaction placeholders
    back into a template would corrupt every subsequent spawn. The gate
    outcome is chained either way.
    """
    from bernstein.core.tokens.sensitive_gate import (
        GateConfig,
        emit_gate_audit,
        scan_for_sensitive_content,
    )

    decision = scan_for_sensitive_content(text, config=GateConfig.from_defaults())
    emit_gate_audit(decision, chain=chain, task_id=f"template-compress:{role}", session_id=AUDIT_ACTOR)
    # ``allow`` covers clean input and operator-allowlisted suppressions;
    # any live finding (redacted or refused) stops the compression.
    return decision.action != "allow"


def _atomic_write(path: Path, content: bytes, *, expected_sha256: str) -> None:
    """Write *content* atomically and verify the on-disk bytes by readback."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(content)
    tmp.replace(path)
    if hashlib.sha256(path.read_bytes()).hexdigest() != expected_sha256:
        raise BackupIntegrityError(f"readback verification failed after writing {path}")


def _utc_now_iso() -> str:
    return datetime.now(tz=UTC).isoformat()


def compress_role_templates(
    role: str,
    *,
    workdir: Path,
    llm_call: Callable[[str], str],
    adapter: str,
    model: str,
    chain: AuditChainStore | None = None,
    lock_path: Path | None = None,
    backup_root: Path | None = None,
    ts: float | None = None,
) -> CompressionOutcome:
    """Compress one role's prompt templates in place, gated and receipted.

    Pipeline per prompt file: sensitive gate -> frontmatter split ->
    LLM rewrite via *llm_call* -> full validator suite with up to two
    targeted fix passes -> token-savings check. Nothing touches disk
    until every file validated; then originals are backed up out of
    tree (readback-verified), the compressed files are written
    atomically (readback-verified), the receipt is chained, and the
    ``templates.lock`` row is appended.

    Args:
        role: Role template directory name (e.g. ``backend``).
        workdir: Project root; templates resolve like the drift check.
        llm_call: Callable sending one prompt to the configured adapter
            and returning the response text. Used for both the rewrite
            and the targeted fix passes.
        adapter: Adapter/provider name recorded in the receipt.
        model: Model name recorded in the receipt.
        chain: Audit chain for the receipt and gate events; resolved
            callers may pass ``None`` to skip chain anchoring.
        lock_path: ``templates.lock`` override (defaults to
            ``<workdir>/templates.lock``).
        backup_root: Backup directory override (tests).
        ts: Explicit receipt timestamp; ``time.time()`` when omitted.

    Returns:
        A :class:`CompressionOutcome`; ``applied`` is False when the
        compression was refused, failed validation, or saved nothing,
        and in every such case the on-disk templates are untouched.

    Raises:
        TemplateCompressionError: When the role directory does not
            exist, is not project-local, or has no prompt files.
    """
    from bernstein.core.teams.drift import resolve_roles_dir, role_template_digest

    if role.startswith("_"):
        raise TemplateCompressionError(f"role {role!r} is not a compressible role template")
    roles_dir = resolve_roles_dir(workdir)
    if not _is_project_local(roles_dir, workdir):
        raise TemplateCompressionError(
            f"role templates resolve to the bundled package copy ({roles_dir}); "
            "compression rewrites files in place and requires project-local templates "
            "(templates/roles/ under the project root)"
        )
    role_dir = roles_dir / role
    if not role_dir.is_dir():
        raise TemplateCompressionError(f"role template directory not found: {role_dir}")

    prompt_files = [role_dir / name for name in TEMPLATE_PROMPT_FILENAMES if (role_dir / name).is_file()]
    if not prompt_files:
        raise TemplateCompressionError(f"role {role!r} has no prompt files to compress under {role_dir}")

    pre_dir_digest = role_template_digest(role_dir)

    rewritten: list[tuple[Path, bytes, bytes]] = []  # (path, original, compressed)
    merged_verdicts: dict[str, bool] = {}
    retry_total = 0
    for path in prompt_files:
        original_bytes = path.read_bytes()
        original_text = original_bytes.decode("utf-8")

        if _gate_refuses(original_text, role=role, chain=chain):
            return CompressionOutcome(
                role=role,
                applied=False,
                reason=f"sensitive gate refused compression of {path.name}; template left untouched",
            )

        frontmatter, body = split_frontmatter(original_text)
        try:
            candidate_body = _extract_rewrite(llm_call(build_compression_prompt(body)))
        except Exception as exc:
            return CompressionOutcome(
                role=role,
                applied=False,
                reason=f"adapter call failed for {path.name}: {exc}",
            )
        candidate = frontmatter + candidate_body
        # Adapters commonly trim trailing whitespace; keep the file
        # POSIX-clean when the original ended with a newline.
        if original_text.endswith("\n") and not candidate.endswith("\n"):
            candidate += "\n"

        outcome = validate_template_rewrite(original_text, candidate, fix_call=llm_call)
        retry_total += outcome.retry_count
        if not outcome.passed:
            failed = ", ".join(v.name for v in outcome.verdicts if not v.passed)
            return CompressionOutcome(
                role=role,
                applied=False,
                reason=(
                    f"validators rejected the rewrite of {path.name} after "
                    f"{outcome.retry_count} fix pass(es): {failed}; template left untouched"
                ),
            )
        for verdict in outcome.verdicts:
            merged_verdicts[verdict.name] = merged_verdicts.get(verdict.name, True) and verdict.passed

        compressed_bytes = outcome.text.encode("utf-8")
        if len(compressed_bytes) < len(original_bytes):
            rewritten.append((path, original_bytes, compressed_bytes))

    if not rewritten:
        return CompressionOutcome(role=role, applied=False, reason="rewrite saved no tokens; template left untouched")

    pre_tokens = sum(
        estimate_tokens_for_text(original.decode("utf-8"), _TOKEN_ESTIMATE_TYPE) for _, original, _ in rewritten
    )
    post_tokens = sum(
        estimate_tokens_for_text(compressed.decode("utf-8"), _TOKEN_ESTIMATE_TYPE) for _, _, compressed in rewritten
    )

    # Backups first (out of tree, readback-verified); nothing in the
    # role directory changes until every backup is durable.
    file_records: list[CompressedFileRecord] = []
    for path, original_bytes, compressed_bytes in rewritten:
        pre_sha = store_backup(original_bytes, backup_root=backup_root)
        file_records.append(
            CompressedFileRecord(
                path=path.relative_to(role_dir).as_posix(),
                pre_sha256=pre_sha,
                post_sha256=hashlib.sha256(compressed_bytes).hexdigest(),
            )
        )

    written: list[tuple[Path, bytes]] = []
    try:
        for (path, original_bytes, compressed_bytes), record in zip(rewritten, file_records, strict=True):
            _atomic_write(path, compressed_bytes, expected_sha256=record.post_sha256)
            written.append((path, original_bytes))
    except Exception:
        # Roll back any partial write from the in-memory originals so
        # the role directory is byte-identical to its pre-state.
        for path, original_bytes in written:
            path.write_bytes(original_bytes)
        raise

    post_dir_digest = role_template_digest(role_dir)

    receipt = TemplateCompressionReceipt(
        role=role,
        pre_sha256=pre_dir_digest,
        post_sha256=post_dir_digest,
        pre_tokens=pre_tokens,
        post_tokens=post_tokens,
        validators=tuple(merged_verdicts.items()),
        adapter=adapter,
        model=model,
        retry_count=retry_total,
        files=tuple(file_records),
        ts=ts if ts is not None else time.time(),
        correlation_id=f"tmplc-{uuid.uuid4().hex[:8]}",
    )

    chain_head = ""
    if chain is not None:
        try:
            event = record_compression_receipt(chain=chain, receipt=receipt)
            chain_head = event.hmac or ""
        except Exception as exc:
            logger.warning("Template compression receipt chain write failed for role %s: %s", role, exc)

    entry = CompressionLockEntry(
        role=role,
        correlation_id=receipt.correlation_id,
        pre_digest=pre_dir_digest,
        post_digest=post_dir_digest,
        adapter=adapter,
        model=model,
        chain_head=chain_head,
        timestamp=_utc_now_iso(),
        files=tuple(file_records),
    )
    append_compression_entry(lock_path if lock_path is not None else templates_lock_path(workdir), entry)

    return CompressionOutcome(
        role=role,
        applied=True,
        receipt=receipt,
        pre_tokens=pre_tokens,
        post_tokens=post_tokens,
    )


def _is_project_local(roles_dir: Path, workdir: Path) -> bool:
    """Whether *roles_dir* lives under *workdir* (never the bundled copy)."""
    try:
        roles_dir.resolve().relative_to(workdir.resolve())
    except ValueError:
        return False
    return True


# ---------------------------------------------------------------------------
# Restore
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RestoreOutcome:
    """Result of a role restore.

    Attributes:
        role: The restored role.
        restored_files: Relative paths written back byte-identically.
        pre_digest: Role directory digest after restore (equals the
            digest recorded before compression; hash-verified).
        correlation_id: The reversed compression's correlation id.
    """

    role: str
    restored_files: tuple[str, ...]
    pre_digest: str
    correlation_id: str


def restore_role_templates(
    role: str,
    *,
    workdir: Path,
    lock_path: Path | None = None,
    backup_root: Path | None = None,
    chain: AuditChainStore | None = None,
) -> RestoreOutcome:
    """Reverse the most recent receipted compression of *role* byte-identically.

    Every step is hash-verified: the on-disk files must match the lock
    row's post hashes (refusing to clobber manual edits), the backups
    must hash to their keys, and after the write the role directory
    digest must equal the pre-compression digest recorded in the lock.

    Args:
        role: Role to restore.
        workdir: Project root.
        lock_path: ``templates.lock`` override.
        backup_root: Backup directory override (tests).
        chain: Optional audit chain receiving a
            ``template.compression.restore`` event.

    Returns:
        A :class:`RestoreOutcome` with the verified pre-digest.

    Raises:
        TemplateCompressionError: When no receipted compression exists
            for *role*, the on-disk templates diverged from the lock
            row, or a hash verification fails.
    """
    from bernstein.core.teams.drift import resolve_roles_dir, role_template_digest

    resolved_lock = lock_path if lock_path is not None else templates_lock_path(workdir)
    state = read_templates_lock(resolved_lock)
    entries = state.entries_for(role)
    if not entries:
        raise TemplateCompressionError(f"no receipted compression found for role {role!r} in {resolved_lock}")
    entry = entries[-1]

    role_dir = resolve_roles_dir(workdir) / role
    if not role_dir.is_dir():
        raise TemplateCompressionError(f"role template directory not found: {role_dir}")

    # Refuse when the on-disk state is not the compressed state the lock
    # row describes - restoring over manual edits would destroy them.
    for record in entry.files:
        target = role_dir / record.path
        if not target.is_file():
            raise TemplateCompressionError(f"compressed file missing on disk: {target}")
        current = hashlib.sha256(target.read_bytes()).hexdigest()
        if current != record.post_sha256:
            raise TemplateCompressionError(
                f"{target} was modified after compression (sha256 {current[:12]}... != "
                f"recorded {record.post_sha256[:12]}...); refusing to restore over it"
            )
    # The whole directory must be in the recorded post-compression state
    # (catches edits to files the compression never touched, such as
    # config.yaml); only then does restoring the prompt files provably
    # reproduce the recorded pre-compression digest.
    current_dir_digest = role_template_digest(role_dir)
    if current_dir_digest != entry.post_digest:
        raise TemplateCompressionError(
            f"role template directory changed since compression (digest "
            f"{current_dir_digest[:12]}... != recorded {entry.post_digest[:12]}...); "
            "refusing to restore over it"
        )

    originals = {record.path: read_backup(record.pre_sha256, backup_root=backup_root) for record in entry.files}
    for record in entry.files:
        _atomic_write(role_dir / record.path, originals[record.path], expected_sha256=record.pre_sha256)

    post_restore_digest = role_template_digest(role_dir)
    if post_restore_digest != entry.pre_digest:
        raise TemplateCompressionError(
            f"restore verification failed for role {role!r}: directory digest "
            f"{post_restore_digest[:12]}... != recorded pre-compression digest {entry.pre_digest[:12]}..."
        )

    remove_compression_entry(resolved_lock, entry.correlation_id)

    if chain is not None:
        try:
            from bernstein.core.security.audit_chain import EVENT_TEMPLATE_COMPRESSION_RESTORE

            chain.log_with_prev_digest(
                event_type=EVENT_TEMPLATE_COMPRESSION_RESTORE,
                actor=AUDIT_ACTOR,
                resource_type=AUDIT_RESOURCE_TYPE,
                resource_id=role,
                details={
                    "role": role,
                    "correlation_id": entry.correlation_id,
                    "restored_pre_sha256": entry.pre_digest,
                    "from_post_sha256": entry.post_digest,
                },
            )
        except Exception as exc:
            logger.warning("Template restore chain write failed for role %s: %s", role, exc)

    return RestoreOutcome(
        role=role,
        restored_files=tuple(record.path for record in entry.files),
        pre_digest=post_restore_digest,
        correlation_id=entry.correlation_id,
    )


# ---------------------------------------------------------------------------
# Drift recognition helper (consumed by bernstein.core.teams.drift)
# ---------------------------------------------------------------------------


def compression_explains_digest_change(
    lock_state: TemplatesLockState,
    *,
    role: str,
    pinned_digest: str,
    actual_digest: str,
) -> bool:
    """Whether receipted compressions turn *pinned_digest* into *actual_digest*.

    Walks the ``pre_digest -> post_digest`` edges recorded for *role*
    (repeat compressions chain), so the team-manifest drift check can
    classify the divergence as intentional rather than drift.

    Args:
        lock_state: Parsed ``templates.lock``.
        role: The pinned role being checked.
        pinned_digest: The manifest's pinned role template digest.
        actual_digest: The on-disk role template digest.

    Returns:
        True when a chain of receipted compressions leads from the
        pinned digest to the actual digest.
    """
    if not _HEX_DIGEST_RE.match(pinned_digest) or not _HEX_DIGEST_RE.match(actual_digest):
        return False
    edges: dict[str, set[str]] = {}
    for row in lock_state.entries_for(role):
        edges.setdefault(row.pre_digest, set()).add(row.post_digest)
    seen: set[str] = set()
    frontier = [pinned_digest]
    while frontier:
        digest = frontier.pop()
        if digest in seen:
            continue
        seen.add(digest)
        for nxt in edges.get(digest, ()):
            if nxt == actual_digest:
                return True
            frontier.append(nxt)
    return False


__all__ = [
    "AUDIT_ACTOR",
    "AUDIT_RESOURCE_TYPE",
    "TEMPLATES_LOCK_FILENAME",
    "TEMPLATE_PROMPT_FILENAMES",
    "BackupIntegrityError",
    "CompressedFileRecord",
    "CompressionLockEntry",
    "CompressionOutcome",
    "RestoreOutcome",
    "TemplateCompressionError",
    "TemplateCompressionReceipt",
    "TemplatesLockState",
    "append_compression_entry",
    "build_compression_prompt",
    "compress_role_templates",
    "compression_explains_digest_change",
    "default_backup_root",
    "load_compression_receipts",
    "read_backup",
    "read_templates_lock",
    "receipt_from_details",
    "record_compression_receipt",
    "remove_compression_entry",
    "restore_role_templates",
    "store_backup",
    "templates_lock_path",
    "verify_compression_receipts",
    "write_templates_lock",
]
