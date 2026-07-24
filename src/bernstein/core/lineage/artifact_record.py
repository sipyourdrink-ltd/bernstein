"""Record and verify a non-coding artifact as a signed lineage entry (#2608).

Slice 1 gives a non-coding task's output the same guarantees a code diff gets:
the artifact's canonical bytes are recorded through :class:`LineageRecorder`,
so the entry carries an HMAC envelope + Ed25519 detached-JWS signature, and its
identity is ``content_hash = sha256(canonical_bytes)``. The recorded, signed
entry *is* the artifact - strip lineage/signing/canonicalisation and there is
only an unattested blob no operator can prove the agent produced.

The write side (:func:`record_artifact`) is a thin wrapper over
``record_write``: it canonicalises the raw artifact under its
:class:`ArtifactKind`, records it, and persists the canonical bytes plus a
small receipt to a content sink so the verifier can re-derive the hash. The
entry is a *deterministic projection* of ``(task_id, kind, artifact)``: the
tool-call id, span id, and timestamp are derived from the task, so two
operators with equal inputs produce a byte-identical signed entry.

The read side (:func:`verify_artifact`) re-derives the canonical hash from the
stored bytes, ties it to the signed entry, and runs the lineage gate
(signature + HMAC chain + parent integrity). It fails on any post-hoc byte
alteration or a removed entry.

This module intentionally does not touch the adapter ``output_mode`` axis,
worktree allocation, or the ``commit_completion`` branch - those are slice 2.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from bernstein.core.lineage.entry import entry_hash
from bernstein.core.lineage.gate import check
from bernstein.core.tasks.artifacts import (
    ArtifactKind,
    CanonicalisationError,
    canonicalise_artifact,
    content_hash,
)
from bernstein.core.tasks.figures import (
    ReportBundle,
    canonicalise_report_bundle,
    is_report_bundle,
)

if TYPE_CHECKING:
    from pathlib import Path

    from bernstein.core.lineage.identity import AgentCard
    from bernstein.core.lineage.recorder import LineageRecorder
    from bernstein.core.tasks.figures import FiguresVerdict, TokenizerPolicy

#: Logical, repo-relative POSIX prefix under which artifact entries are anchored
#: in the lineage log. The physical sink root is supplied separately so tests
#: and operators can point it anywhere while the *entry* path stays stable.
ARTIFACT_SINK_RELPATH = ".sdd/artifacts"

_BLOB_NAME = "artifact.bin"
_RECEIPT_NAME = "receipt.json"


def artifact_entry_path(task_id: str) -> str:
    """Return the repo-relative lineage path an artifact for ``task_id`` anchors at."""
    return f"{ARTIFACT_SINK_RELPATH}/{task_id}/{_BLOB_NAME}"


def _artifact_tool_call_id(task_id: str) -> str:
    return f"artifact:{task_id}"


def _artifact_span_id(task_id: str, kind: str) -> str:
    return hashlib.sha256(f"artifact:{task_id}:{kind}".encode()).hexdigest()[:16]


@dataclass(frozen=True)
class ArtifactReceipt:
    """The completion receipt for an artifact-mode task.

    The receipt is *not* the trust anchor - the signed lineage entry is. It is
    a pointer that lets the verifier find the entry and the stored bytes and
    re-derive the hash; every field is re-checked against the signed log.
    """

    task_id: str
    kind: str
    content_hash: str
    entry_hash: str
    artefact_path: str
    agent_id: str
    agent_card_kid: str
    ts_ns: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "kind": self.kind,
            "content_hash": self.content_hash,
            "entry_hash": self.entry_hash,
            "artefact_path": self.artefact_path,
            "agent_id": self.agent_id,
            "agent_card_kid": self.agent_card_kid,
            "ts_ns": self.ts_ns,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ArtifactReceipt:
        return cls(
            task_id=str(data["task_id"]),
            kind=str(data["kind"]),
            content_hash=str(data["content_hash"]),
            entry_hash=str(data["entry_hash"]),
            artefact_path=str(data["artefact_path"]),
            agent_id=str(data["agent_id"]),
            agent_card_kid=str(data["agent_card_kid"]),
            ts_ns=int(data["ts_ns"]),
        )


@dataclass(frozen=True)
class ArtifactVerifyResult:
    """Outcome of :func:`verify_artifact`. ``ok`` is True iff ``failures`` is empty.

    ``figures`` is populated only when the stored bytes are a report bundle with
    a ``figures.json`` sidecar (issue #2888): it carries the per-figure
    provenance statements and any unanchored numbers. A failing figure verdict
    contributes to ``failures`` and flips ``ok``.
    """

    task_id: str
    ok: bool
    failures: list[str]
    content_hash: str | None
    entry_hash: str | None
    figures: FiguresVerdict | None = None


def record_artifact(
    *,
    recorder: LineageRecorder,
    sink_root: Path,
    task_id: str,
    kind: ArtifactKind | str,
    artifact: Any,
    agent_id: str,
    agent_card: AgentCard,
    private_key_pem: str,
    ts_ns: int = 0,
) -> ArtifactReceipt:
    """Canonicalise, record, and persist a non-coding artifact.

    Returns the :class:`ArtifactReceipt` whose ``entry_hash`` is the completion
    receipt for the task (the signed lineage-entry hash, not a git SHA).

    Args:
        recorder: The lineage recorder to append the signed entry through.
        sink_root: Physical directory the canonical bytes + receipt are written
            under (``<sink_root>/<task_id>/``).
        task_id: The task the artifact completes.
        kind: The artifact's :class:`ArtifactKind`; ``code_diff`` is rejected -
            it belongs on the git-diff path, not the artifact sink.
        artifact: The raw produced artifact (str / bytes / list / mapping).
        agent_id, agent_card, private_key_pem: Signing identity.
        ts_ns: Logical entry timestamp. Fixed (default ``0``) so the signed
            entry is a deterministic projection of the inputs.

    Raises:
        ValueError: When ``kind`` is ``code_diff``.
        CanonicalisationError: When ``artifact`` does not fit the kind's rule.
    """
    k = ArtifactKind(kind)
    if k is ArtifactKind.CODE_DIFF:
        raise ValueError("code_diff artifacts use the git-diff path, not the artifact sink")

    # A report with a figures sidecar is recorded as a single canonical bundle
    # (body + figures.json) so the sidecar is inside the artifact content_hash
    # (issue #2888). Any other artifact routes through its kind's canonicaliser.
    if isinstance(artifact, ReportBundle):
        if k is not ArtifactKind.REPORT:
            raise ValueError(f"a ReportBundle must be recorded as a report kind, not {k.value!r}")
        canonical = canonicalise_report_bundle(artifact)
    else:
        canonical = canonicalise_artifact(k, artifact)
    chash = content_hash(canonical)
    artefact_path = artifact_entry_path(task_id)

    eh = recorder.record_write(
        artefact_path=artefact_path,
        new_content=canonical,
        agent_id=agent_id,
        agent_card=agent_card,
        private_key_pem=private_key_pem,
        tool_call_id=_artifact_tool_call_id(task_id),
        span_id=_artifact_span_id(task_id, k.value),
        artefact_kind=k.value,
        ts_ns=ts_ns,
    )

    receipt = ArtifactReceipt(
        task_id=task_id,
        kind=k.value,
        content_hash=chash,
        entry_hash=eh,
        artefact_path=artefact_path,
        agent_id=agent_id,
        agent_card_kid=agent_card.kid,
        ts_ns=ts_ns,
    )

    task_dir = sink_root / task_id
    task_dir.mkdir(parents=True, exist_ok=True)
    (task_dir / _BLOB_NAME).write_bytes(canonical)
    (task_dir / _RECEIPT_NAME).write_text(json.dumps(receipt.to_dict(), sort_keys=True, indent=2), encoding="utf-8")
    return receipt


def load_receipt(sink_root: Path, task_id: str) -> ArtifactReceipt | None:
    """Return the persisted receipt for ``task_id``, or ``None`` when absent."""
    receipt_path = sink_root / task_id / _RECEIPT_NAME
    if not receipt_path.exists():
        return None
    return ArtifactReceipt.from_dict(json.loads(receipt_path.read_text(encoding="utf-8")))


def verify_artifact(
    *,
    task_id: str,
    sink_root: Path,
    log_path: Path,
    cards_dir: Path,
    operator_secret: bytes | None,
    policy: TokenizerPolicy | None = None,
) -> ArtifactVerifyResult:
    """Verify a recorded artifact end to end.

    The checks, in order:

    1. **Re-derive the canonical hash** from the stored bytes and confirm it
       matches the receipt - a post-hoc byte alteration of the blob fails here.
    2. **Tie the blob to the signed entry**: the entry named by the receipt
       must exist in the log and its recorded ``content_hash`` must equal the
       re-derived hash - a removed entry or a swapped hash fails here.
    3. **Lineage gate**: every entry's Ed25519 signature verifies, the operator
       HMAC chain is intact, and no ``parent_hash`` dangles - a tampered log
       line or a removed anchor fails here. When ``operator_secret`` is ``None``
       the HMAC leg is skipped (signature + chain still enforced), matching the
       lineage gate's own optional-secret semantics.
    4. **Figure grounding** (issue #2888): when the stored bytes are a report
       bundle with a ``figures.json`` sidecar, every declared figure's anchor
       must resolve to a verifying lineage record and every material number in
       the body must be declared. Any failing figure is a verification failure.

    Returns an :class:`ArtifactVerifyResult`; ``ok`` is True only when every
    check passes.
    """
    failures: list[str] = []
    receipt = load_receipt(sink_root, task_id)
    if receipt is None:
        return ArtifactVerifyResult(task_id, False, [f"no artifact receipt for task {task_id!r}"], None, None)

    blob_path = sink_root / task_id / _BLOB_NAME
    rederived: str | None = None
    stored: bytes | None = None
    if not blob_path.exists():
        failures.append("stored artifact bytes are missing")
    else:
        stored = blob_path.read_bytes()
        rederived = content_hash(stored)
        if rederived != receipt.content_hash:
            failures.append(f"stored bytes altered: re-derived {rederived} != receipt {receipt.content_hash}")

    matched = None
    for entry, _jws in _read_log(log_path):
        if entry.artefact_path == receipt.artefact_path and entry_hash(entry) == receipt.entry_hash:
            matched = entry
            break
    if matched is None:
        failures.append(f"lineage entry {receipt.entry_hash} for the artifact is missing from the log")
    elif rederived is not None and matched.content_hash != rederived:
        failures.append(f"recorded content_hash {matched.content_hash} != stored bytes {rederived}")

    gate_result = check(log_path, cards_dir, operator_secret=operator_secret)
    if not gate_result.ok:
        failures.extend(gate_result.failures)

    figures = _figures_verdict(stored, log_path, cards_dir, operator_secret, policy)
    if figures is not None and not figures.ok:
        failures.extend(figures.failures)

    return ArtifactVerifyResult(task_id, not failures, failures, rederived, receipt.entry_hash, figures)


def _figures_verdict(
    stored: bytes | None,
    log_path: Path,
    cards_dir: Path,
    operator_secret: bytes | None,
    policy: TokenizerPolicy | None,
) -> FiguresVerdict | None:
    """Run figure grounding when ``stored`` is a report bundle; else ``None``.

    ``artifact verify`` is the audit tool, so grounding here is unconditional
    (strict): the per-task ``warn`` downgrade is a *completion* concept, not a
    verification one - a verifier always reports a failing figure.
    """
    if stored is None or not is_report_bundle(stored):
        return None
    from bernstein.core.lineage.figure_grounding import verify_report_figures

    return verify_report_figures(
        canonical_bytes=stored,
        log_path=log_path,
        cards_dir=cards_dir,
        operator_secret=operator_secret,
        policy=policy,
    )


def _read_log(log_path: Path) -> Any:
    """Yield ``(entry, jws)`` over a lineage log rooted at ``log_path``.

    A thin adapter over :class:`LineageStore` so the verifier reads through the
    same reconstruction path the recorder wrote through - the store re-derives
    signatures from the sidecar tree keyed by entry hash.
    """
    from bernstein.core.lineage.store import LineageStore

    store = LineageStore(log_path.parent)
    return store.read_log()


def canonicalise_for_kind(kind: ArtifactKind | str, artifact: Any) -> bytes:
    """Public re-export so the CLI can re-derive bytes without importing tasks.

    Raises :class:`CanonicalisationError` on a shape/policy violation.
    """
    return canonicalise_artifact(kind, artifact)


__all__ = [
    "ARTIFACT_SINK_RELPATH",
    "ArtifactReceipt",
    "ArtifactVerifyResult",
    "CanonicalisationError",
    "artifact_entry_path",
    "canonicalise_for_kind",
    "load_receipt",
    "record_artifact",
    "verify_artifact",
]
