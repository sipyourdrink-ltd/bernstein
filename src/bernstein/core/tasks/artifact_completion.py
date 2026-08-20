"""Artifact-mode task completion: a signed receipt instead of a git SHA (#2608).

A coding task completes when workspace HEAD moves - the git SHA *is* the
completion identity, and the whole verification chain hangs off it. A
non-coding task has no commit to point at. Slice 1 gave its output a canonical
form and a recording library (:mod:`bernstein.core.lineage.artifact_record`);
this module is the execution path that actually uses them, so a task declaring
a ``report`` / ``dataset`` / ``action_log`` / ``ops_result`` artifact can reach
``done``.

The completion identity of such a task is the **signed lineage entry hash** of
its artifact's canonical bytes. That is not a log line emitted alongside the
result: it is the result's only identity. Strip the canonicalisation and two
operators disagree on the bytes; strip the signature and nobody can attribute
them; strip the HMAC chain and they can be swapped after the fact. What remains
is an unattributed blob and a task that cannot be said to have completed.

Order of operations, and why it is that order
---------------------------------------------

1. **Load** the produced artifact from the workdir-relative path the task's
   :class:`~bernstein.core.tasks.artifacts.ArtifactSpec` declares, and shape it
   for its kind (JSONL rows, a JSON object, report text, or a figures bundle).
2. **Evaluate every declared completion signal** with the artifact in scope:
   artifact-mode criteria through
   :func:`~bernstein.core.quality.janitor.evaluate_artifact_signals`, the
   filesystem-oriented ones through
   :func:`~bernstein.core.quality.janitor.evaluate_signal`. Issue #2968 made
   the artifact criteria fail closed when nothing evaluated them; this path is
   the evaluator that makes the closed case reachable rather than a dead end.
3. **Record only on a full pass.** A receipt is a claim that the declared gates
   held, so a failing gate must never mint one. A failed artifact task returns
   its failures and no receipt, exactly as a failed coding task returns no
   commit.

Determinism
-----------

The recorded entry is a deterministic projection of
``(task_id, kind, artifact bytes, operator secret)``. The tool-call id and span
id derive from the task id, ``ts_ns`` is a fixed logical timestamp, and the
signing identity contributes only its ``agent_id``/``kid`` (both constants) to
the entry body - the private key signs a detached sidecar and never enters the
hashed bytes. So one operator running the same artifact-mode task twice gets
byte-identical canonical bytes, an identical ``content_hash``, and an identical
``entry_hash``. Hash divergence between two runs is a detected non-determinism,
not a flaky assertion.

The lineage log itself stays append-only, so recording the *same* task twice
into the *same* store produces a second entry chained to the first: identical
content, a different ``entry_hash`` because its ``parent_hashes`` differ. That
is the chain working as intended, and the determinism claim is over the run,
not over repeated appends to one log.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from bernstein.core.security.path_containment import (
    PathContainmentError,
    contained_subpath,
)
from bernstein.core.tasks.artifacts import (
    ArtifactKind,
    ArtifactSpecError,
    CanonicalisationError,
    validate_artifact_output_path,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from bernstein.core.lineage.artifact_record import ArtifactReceipt
    from bernstein.core.lineage.identity import AgentCard
    from bernstein.core.lineage.signed_write import SignedLineageLog
    from bernstein.core.tasks.models import Task

logger = logging.getLogger(__name__)

#: Workdir-relative prefix an artifact-mode task writes its output under when
#: its :class:`ArtifactSpec` declares no explicit ``output_path``. Kept distinct
#: from the lineage sink (``.sdd/artifacts``) so the agent-produced bytes and
#: the recorded canonical bytes never occupy the same path.
DEFAULT_OUTBOX_RELPATH = ".sdd/outbox"

#: Stable signing identity for artifact receipts. Both values are *constants*
#: on purpose: they are inside the hashed entry body, so pinning them is what
#: makes the entry hash reproducible across installs that hold different
#: private keys. The key material differs per install; the identity does not.
ARTIFACT_AGENT_ID = "agent:artifact-completion"
ARTIFACT_KID = "artifact-completion-1"

_PRIVATE_KEY_NAME = "artifact_lineage.pem"
_PUBLIC_KEY_NAME = "artifact_lineage.pub"

#: Fixed logical entry timestamp. A wall clock would make every run's entry
#: hash unique and destroy the determinism guarantee this contract is for.
ARTIFACT_TS_NS = 0


class ArtifactCompletionError(RuntimeError):
    """Raised when an artifact-mode task cannot be evaluated at all.

    Distinct from "the artifact failed its criteria": this is a task that
    declared an artifact contract it did not honour (no output written, bytes
    that do not fit the declared kind, an unsafe output path). Callers surface
    it as a completion failure with the message attached.
    """


@dataclass(frozen=True)
class ArtifactCompletion:
    """Outcome of running an artifact-mode task's completion path.

    Attributes:
        task_id: The task evaluated.
        ok: ``True`` iff every declared signal passed *and* the receipt was
            recorded. A receipt only exists when ``ok`` is ``True``.
        failures: Operator-facing descriptions of every failing signal, in the
            ``"<type>: <value> (<detail>)"`` shape ``verify_task`` produces, so
            the existing retry/fail surfaces render them unchanged.
        signal_results: ``(description, passed, detail)`` per evaluated signal.
        receipt: The signed completion receipt, or ``None`` when the task did
            not pass. ``receipt.entry_hash`` is the completion identity that
            stands in for a coding task's git SHA.
    """

    task_id: str
    ok: bool
    failures: list[str] = field(default_factory=list[str])
    signal_results: list[tuple[str, bool, str]] = field(default_factory=list[tuple[str, bool, str]])
    receipt: ArtifactReceipt | None = None

    @property
    def entry_hash(self) -> str | None:
        """The signed lineage entry hash, or ``None`` when nothing was recorded."""
        return self.receipt.entry_hash if self.receipt is not None else None

    def as_verify_result(self) -> tuple[bool, list[str]]:
        """Return the ``(passed, failed_descriptions)`` shape callers expect.

        Lets the artifact path drop into every existing ``verify_task`` call
        site without the surrounding completion pipeline learning a new type.
        """
        return self.ok, list(self.failures)


# ---------------------------------------------------------------------------
# Mode + path resolution
# ---------------------------------------------------------------------------


def is_artifact_mode(task: Task) -> bool:
    """Return ``True`` when ``task`` completes via an artifact, not a commit.

    Any declared kind other than ``code_diff`` is artifact mode. A task with no
    ``artifact_spec`` defaults to ``code_diff``, so every existing coding task
    answers ``False`` and keeps the git path untouched.
    """
    return task.artifact_spec.kind is not ArtifactKind.CODE_DIFF


def needs_git_worktree(tasks: Sequence[Task]) -> bool:
    """Return ``True`` when the session spawned for ``tasks`` needs a git worktree.

    The single decision point for git-vs-plain workspace allocation
    (issue #2996), kept next to :func:`is_artifact_mode` so worktree
    allocation, batch admission, and the completion path all read one
    resolver and the two output modes cannot drift.

    A batch containing any ``code_diff`` task completes through the git path
    (commit, merge-back), so the session must run inside a per-session git
    worktree. A batch that is artifact-mode throughout completes on signed
    lineage receipts and never writes through git - it needs an isolated
    working directory, not a checkout on an agent branch. An empty batch is
    answered conservatively with ``True``: the callers reject empty batches
    before allocation, so the value only matters for not weakening isolation
    if that ever changes.
    """
    if not tasks:
        return True
    return any(not is_artifact_mode(task) for task in tasks)


def artifact_output_path(task: Task) -> str:
    """Return the workdir-relative POSIX path ``task`` writes its artifact to.

    The task's declared ``output_path`` wins; otherwise the per-task default
    under :data:`DEFAULT_OUTBOX_RELPATH` applies, so a task can declare a kind
    and nothing else and still have a well-defined place to write.

    Raises:
        ArtifactCompletionError: The declared path is absolute or escapes the
            workdir. Rejected here, before any bytes are read.
    """
    declared = (task.artifact_spec.output_path or "").strip()
    if not declared:
        return f"{DEFAULT_OUTBOX_RELPATH}/{task.id}/artifact"
    # One set of path rules, shared with the declaration parser (#3110):
    # a declaration that loads is a path this completion check will accept.
    try:
        return validate_artifact_output_path(declared)
    except ArtifactSpecError as exc:
        raise ArtifactCompletionError(f"artifact output_path {exc.reason}") from exc


def _resolve_contained_artifact_path(workdir: Path, relpath: str) -> Path:
    """Resolve ``relpath`` under ``workdir`` and refuse any escape.

    :func:`validate_artifact_output_path` rejects lexical escapes (absolute
    paths, ``..``) at declaration time, but a symlink planted inside the
    workdir can still point outside it, and the produced artifact's bytes are
    about to become the subject of a signed receipt. Containment is therefore
    enforced on the *resolved* path, after every symlink is followed. The
    resolved path, not the declared one, is what gets opened afterwards: a
    component swapped between this check and the read changes which contained
    bytes are read, never whether the read stays inside the workdir.
    """
    try:
        return contained_subpath(workdir, relpath, label="artifact output path")
    except PathContainmentError as exc:
        raise ArtifactCompletionError(
            f"artifact output path {relpath!r} resolves outside the task workdir; refusing to read it"
        ) from exc


# ---------------------------------------------------------------------------
# Loading the produced artifact
# ---------------------------------------------------------------------------


def _rows_from_jsonl(raw: bytes, kind: ArtifactKind) -> list[Any]:
    """Parse JSONL bytes into row objects, tolerating a trailing newline."""
    try:
        decoded = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ArtifactCompletionError(f"{kind.value} artifact is not valid UTF-8: {exc}") from exc
    text = decoded.replace("\r\n", "\n").replace("\r", "\n")
    lines = [line for line in text.split("\n") if line.strip()]
    rows: list[Any] = []
    for i, line in enumerate(lines):
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError as exc:
            raise ArtifactCompletionError(f"{kind.value} artifact line {i + 1} is not valid JSON: {exc}") from exc
    return rows


def load_artifact(task: Task, workdir: Path) -> Any:
    """Read and shape ``task``'s produced artifact from ``workdir``.

    The returned value is the *raw* artifact in the shape the kind's
    canonicaliser and criterion evaluators expect - not canonical bytes. A
    ``report`` whose bytes are a figures bundle is returned as a
    :class:`~bernstein.core.tasks.figures.ReportBundle` so ``figures_grounded``
    can resolve its anchors and so the bundle (body *and* sidecar) is hashed as
    one unit.

    Raises:
        ArtifactCompletionError: No output was written, or the bytes do not fit
            the declared kind.
    """
    relpath = artifact_output_path(task)
    path = _resolve_contained_artifact_path(Path(workdir), relpath)
    try:
        raw = path.read_bytes()
    except (FileNotFoundError, IsADirectoryError, NotADirectoryError):
        raise ArtifactCompletionError(
            f"task declares artifact kind {task.artifact_spec.kind.value!r} but wrote no output at {relpath}"
        ) from None
    kind = task.artifact_spec.kind

    if kind is ArtifactKind.CODE_DIFF:  # pragma: no cover - guarded by is_artifact_mode
        raise ArtifactCompletionError("code_diff tasks complete through the git path, not the artifact sink")

    if kind in (ArtifactKind.DATASET, ArtifactKind.ACTION_LOG):
        return _rows_from_jsonl(raw, kind)

    if kind is ArtifactKind.OPS_RESULT:
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, ValueError) as exc:
            raise ArtifactCompletionError(f"ops_result artifact is not valid JSON: {exc}") from exc

    # report: a figures bundle is loaded as a bundle so the sidecar is inside
    # the content hash (issue #2888); anything else is plain report text.
    from bernstein.core.tasks.figures import is_report_bundle, parse_report_bundle

    if is_report_bundle(raw):
        return parse_report_bundle(raw)
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ArtifactCompletionError(f"report artifact is not valid UTF-8: {exc}") from exc


# ---------------------------------------------------------------------------
# Signing identity + signed log
# ---------------------------------------------------------------------------


def artifact_identity_dir(sdd_dir: Path) -> Path:
    """Return the directory holding the artifact-receipt signing identity."""
    return Path(sdd_dir) / "artifacts" / "identity"


def load_artifact_identity(sdd_dir: Path) -> tuple[AgentCard, str]:
    """Provision (on first use) and return the artifact signing identity.

    Returns ``(agent_card, private_key_pem)``. The card is also published under
    ``<sdd>/agents/<agent_id>/card.json`` where the lineage gate looks for it,
    so a receipt recorded in one invocation verifies in the next without any
    operator key-management step.
    """
    from bernstein.core.lineage.identity import AgentCard, load_or_create_signing_identity

    sdd = Path(sdd_dir)
    private_pem, public_pem = load_or_create_signing_identity(
        artifact_identity_dir(sdd),
        private_name=_PRIVATE_KEY_NAME,
        public_name=_PUBLIC_KEY_NAME,
    )
    card = AgentCard(agent_id=ARTIFACT_AGENT_ID, kid=ARTIFACT_KID, public_key_pem=public_pem)
    _publish_card(sdd / "agents", card)
    return card, private_pem


def _publish_card(cards_dir: Path, card: AgentCard) -> None:
    """Write ``card`` to the ``<agent-id>/card.json`` layout the gate reads."""
    card_dir = cards_dir / card.agent_id
    card_dir.mkdir(parents=True, exist_ok=True)
    (card_dir / "card.json").write_text(
        json.dumps(
            {
                "agent_id": card.agent_id,
                "kid": card.kid,
                "public_key_pem": card.public_key_pem,
                "protocol_version": card.protocol_version,
            },
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )


def _build_signed_log(sdd_dir: Path, operator_hmac_key: bytes) -> SignedLineageLog:
    """Return the signed lineage log rooted at ``<sdd>/lineage``.

    The supported signed-write path: the entry carries an Ed25519 detached JWS
    verifiable offline against the published Agent Card *and* an operator-HMAC
    envelope over every field. Both are what let a receipt stand in for a git
    SHA - an entry anyone can verify and nobody can substitute after the fact.
    """
    from bernstein.core.lineage.signed_write import SignedLineageLog
    from bernstein.core.lineage.store import LineageStore

    return SignedLineageLog(LineageStore(Path(sdd_dir) / "lineage"), operator_hmac_key=operator_hmac_key)


def artifact_sink_root(workdir: Path) -> Path:
    """Return the physical sink the canonical bytes + receipt are stored under.

    Matches the logical entry prefix (``.sdd/artifacts``) so
    ``bernstein artifact verify`` resolves the same tree the completion path
    wrote, with no operator-side configuration.
    """
    from bernstein.core.lineage.artifact_record import ARTIFACT_SINK_RELPATH

    return Path(workdir) / ARTIFACT_SINK_RELPATH


# ---------------------------------------------------------------------------
# The completion path
# ---------------------------------------------------------------------------


def _evaluate_signals(
    task: Task,
    workdir: Path,
    artifact: Any,
    *,
    operator_secret: bytes | None,
) -> list[tuple[str, bool, str]]:
    """Evaluate every declared signal with the artifact in scope.

    Artifact-mode criteria route through ``evaluate_artifact_signals`` (which
    has the artifact); everything else routes through ``evaluate_signal``
    against the filesystem, so an artifact task can still declare, say, a
    ``path_exists`` gate. ``llm_judge`` is skipped for the same reason
    :func:`~bernstein.core.quality.janitor._collect_signal_results` skips it -
    it has no synchronous evaluator.
    """
    from bernstein.core.quality.janitor import (
        ARTIFACT_SIGNAL_TYPES,
        evaluate_artifact_signals,
        evaluate_signal,
    )

    results: list[tuple[str, bool, str]] = list(
        evaluate_artifact_signals(
            task,
            artifact,
            lineage_root=Path(workdir),
            operator_secret=operator_secret,
        )
    )
    for signal in task.completion_signals:
        if signal.type in ARTIFACT_SIGNAL_TYPES or signal.type == "llm_judge":
            continue
        passed, detail = evaluate_signal(signal, Path(workdir))
        results.append((f"{signal.type}: {signal.value}", passed, detail))
    return results


def _describe_failures(results: list[tuple[str, bool, str]]) -> list[str]:
    """Render failing results in the description shape ``verify_task`` emits."""
    return [f"{desc} ({detail})" if detail else desc for desc, passed, detail in results if not passed]


def complete_artifact_task(
    task: Task,
    workdir: Path,
    *,
    sdd_dir: Path | None = None,
    operator_hmac_key: bytes | None = None,
    ts_ns: int = ARTIFACT_TS_NS,
) -> ArtifactCompletion:
    """Run ``task``'s artifact-mode completion and record its signed receipt.

    Args:
        task: An artifact-mode task (see :func:`is_artifact_mode`).
        workdir: Project root. The artifact is read relative to it, the lineage
            log and sink live under its ``.sdd``.
        sdd_dir: Override for the ``.sdd`` root; defaults to ``workdir/.sdd``.
        operator_hmac_key: Override for the operator secret; defaults to the
            install audit key. Supplied explicitly by tests that need two runs
            under one operator identity.
        ts_ns: Logical entry timestamp. Fixed so the entry is a deterministic
            projection of the inputs.

    Returns:
        An :class:`ArtifactCompletion`. ``receipt`` is populated only when every
        declared signal passed.
    """
    sdd = Path(sdd_dir) if sdd_dir is not None else Path(workdir) / ".sdd"

    try:
        artifact = load_artifact(task, workdir)
    except (ArtifactCompletionError, CanonicalisationError) as exc:
        return ArtifactCompletion(task_id=task.id, ok=False, failures=[f"artifact: {exc}"])

    if operator_hmac_key is None:
        from bernstein.core.security.audit import load_or_create_audit_key

        operator_hmac_key = load_or_create_audit_key()

    results = _evaluate_signals(task, workdir, artifact, operator_secret=operator_hmac_key)
    failures = _describe_failures(results)
    if failures:
        # No receipt: a receipt asserts the declared gates held.
        return ArtifactCompletion(task_id=task.id, ok=False, failures=failures, signal_results=results)

    from bernstein.core.lineage.artifact_record import record_artifact

    card, private_pem = load_artifact_identity(sdd)
    # Issue #1797: an artefact produced by a worker that was handed an image
    # records that image's digest on its signed entry, so the receipt names
    # every input the turn saw and not just the code it read. The digests come
    # from what the spawn stamped on the task -- the same records behind the
    # ``multimodal.attach`` chain rows -- rather than a fresh hash of files
    # that may have changed since. Empty for an unattached task, which leaves
    # its entry hash unchanged.
    from bernstein.core.agents.attachment_dispatch import attachment_digests_for_tasks

    try:
        receipt = record_artifact(
            recorder=_build_signed_log(sdd, operator_hmac_key),
            sink_root=artifact_sink_root(workdir),
            task_id=task.id,
            kind=task.artifact_spec.kind,
            artifact=artifact,
            agent_id=card.agent_id,
            agent_card=card,
            private_key_pem=private_pem,
            attachment_digests=attachment_digests_for_tasks([task]) or None,
            ts_ns=ts_ns,
        )
    except (CanonicalisationError, ValueError) as exc:
        return ArtifactCompletion(
            task_id=task.id,
            ok=False,
            failures=[f"artifact: could not record signed receipt: {exc}"],
            signal_results=results,
        )

    logger.info(
        "artifact-completion: task=%s kind=%s content_hash=%s entry_hash=%s",
        task.id,
        receipt.kind,
        receipt.content_hash,
        receipt.entry_hash,
    )
    return ArtifactCompletion(task_id=task.id, ok=True, signal_results=results, receipt=receipt)


def verify_task_completion(task: Task, workdir: Path) -> tuple[bool, list[str]]:
    """Verify ``task``'s completion, dispatching on its declared output mode.

    The single entry point every completion path calls instead of
    :func:`~bernstein.core.quality.janitor.verify_task`: an artifact-mode task
    is evaluated against its produced artifact and recorded as a signed
    receipt; every other task keeps the filesystem/git verification it has
    always had, byte for byte.

    Returns the ``(all_passed, failed_signal_descriptions)`` tuple the rest of
    the completion pipeline already consumes.
    """
    from bernstein.core.quality.janitor import verify_task

    if not is_artifact_mode(task):
        return verify_task(task, workdir)
    try:
        return complete_artifact_task(task, workdir).as_verify_result()
    except Exception as exc:  # pragma: no cover - defensive: never lose a task
        logger.warning("artifact-completion failed for task=%s: %s", task.id, exc)
        return False, [f"artifact: completion path raised {type(exc).__name__}: {exc}"]


__all__ = [
    "ARTIFACT_AGENT_ID",
    "ARTIFACT_KID",
    "ARTIFACT_TS_NS",
    "DEFAULT_OUTBOX_RELPATH",
    "ArtifactCompletion",
    "ArtifactCompletionError",
    "artifact_identity_dir",
    "artifact_output_path",
    "artifact_sink_root",
    "complete_artifact_task",
    "is_artifact_mode",
    "load_artifact",
    "load_artifact_identity",
    "needs_git_worktree",
    "verify_task_completion",
]
