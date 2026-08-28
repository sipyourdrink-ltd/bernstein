"""Merge, push, trace finalization, and reap helpers for spawner.

Free functions that encapsulate merge/push/trace operations.  AgentSpawner
delegates to these from its own methods.
"""

from __future__ import annotations

import json
import logging
import subprocess
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from bernstein.core.git_ops import MergeResult, merge_with_conflict_detection
from bernstein.core.models import AgentBackend, AgentSession
from bernstein.core.prometheus import merge_duration
from bernstein.core.traces import AgentTrace, TraceStore, finalize_trace
from bernstein.plugins.manager import get_plugin_manager

if TYPE_CHECKING:
    from bernstein.core.agents.container import ContainerManager
    from bernstein.core.agents.in_process_agent import InProcessAgent
    from bernstein.core.agents.warm_pool import PoolSlot, WarmPool
    from bernstein.core.merge_queue import MergeQueue
    from bernstein.core.quality.quality_gates import QualityGatesConfig
    from bernstein.core.worktree import WorktreeManager

logger = logging.getLogger(__name__)


#: Explicit, opt-in escape hatch that lets agent worktree merges land on the
#: repository's default (protected) branch. Off by default: agent output must
#: not reach a protected trunk without an explicit target. Accepts the
#: project's standard truthy words.
ENV_ALLOW_MERGE_TO_DEFAULT_BRANCH = "BERNSTEIN_ALLOW_MERGE_TO_DEFAULT_BRANCH"

_TRUTHY_ALLOW = frozenset({"1", "true", "yes", "on", "enable", "enabled"})


def _allow_merge_to_default_branch(env: dict[str, str] | None = None) -> bool:
    """Return ``True`` only when the operator explicitly opted into merging
    agent work onto the repository's default branch."""
    import os

    source = os.environ if env is None else env
    raw = source.get(ENV_ALLOW_MERGE_TO_DEFAULT_BRANCH)
    return raw is not None and raw.strip().lower() in _TRUTHY_ALLOW


def _record_merge_refusal(
    worktree_root: Path,
    session_id: str,
    branch: str,
    reason: str = "target-is-default-branch",
) -> None:
    """Persist a visible record that a merge was refused.

    Written to ``.sdd/runtime/refused_merges.jsonl`` next to the pending-push
    journal so an operator (and any postmortem) can see exactly which session
    was blocked, on which branch, why, and when.
    """
    import json as _json

    entry = {
        "session_id": _sanitise_for_log(session_id),
        "branch": _sanitise_for_log(branch),
        "reason": _sanitise_for_log(reason),
        "ts": time.time(),
    }
    try:
        path = worktree_root / ".sdd" / "runtime" / "refused_merges.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(_json.dumps(entry) + "\n")
    except OSError as exc:  # recording is best-effort; never mask the refusal
        logger.debug("Could not record merge refusal for %s: %s", _sanitise_for_log(session_id), exc)


def _sanitise_for_log(value: str) -> str:
    """Strip CR/LF from ``value`` so attacker-controlled input cannot
    inject fake log lines.

    Used at every log site that touches data read out of the pending
    pushes file or subprocess stderr (CodeQL/Sonar py/log-injection
    S5145). Keep this function cheap and side-effect-free -- it is
    called inside the spawner hot path.
    """
    return value.replace("\r", "").replace("\n", "") if value else value


# ---------------------------------------------------------------------------
# Blast-radius reversibility gate (issue #1322, wired in by #3135)
# ---------------------------------------------------------------------------


class IncomingChangeUnreadable(RuntimeError):
    """The change a merge would bring in could not be computed.

    Raised rather than collapsed into an empty change, because the gates
    below judge the change by what is in it: no file outside the scope, no
    score above the ceiling. An empty list answers both of those questions
    with "nothing to object to", which is exactly what a genuinely empty
    change answers -- so a read that failed would be indistinguishable from
    a merge that brings in nothing, and would pass every gate on evidence
    that was never gathered.

    The benign case is a branch that does not exist, where the merge would
    fail anyway and nothing lands. The one that matters is the timeout: the
    file list is read with a 30-second budget the merge itself does not
    share, so a large enough diff can time out here while ``git merge``
    still succeeds.
    """


def _incoming_files(worktree_root: Path, branch: str) -> list[str]:
    """Return the paths merging ``branch`` would touch.

    ``--no-renames``, because rename detection reports only a rename's
    *destination*. Every gate reading this list judges paths, and a list that
    omits the path a merge removes lets ``git mv outside inside`` pass a
    check that ``git rm outside`` fails -- the disguised removal admitted and
    the honest one refused. A rename is two paths changing, so both are named.

    Raises:
        IncomingChangeUnreadable: The file list could not be read. An empty
            list is a change that touches nothing; this is not that.
    """
    from bernstein.core.git_ops import run_git

    spec = f"HEAD...{branch}"
    try:
        names = run_git(["diff", "--name-only", "--no-renames", spec], worktree_root, timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        # ``run_git`` raises ``TimeoutExpired`` rather than returning non-zero,
        # so the case this gate exists for arrives as an exception.
        msg = f"could not be read: git diff --name-only {spec} did not complete ({exc})"
        raise IncomingChangeUnreadable(msg) from exc
    if names.returncode != 0:
        detail = _sanitise_for_log(names.stderr.strip())
        msg = f"could not be read: git diff --name-only {spec} exited {names.returncode} ({detail})"
        raise IncomingChangeUnreadable(msg)
    return [line.strip() for line in names.stdout.splitlines() if line.strip()]


def _incoming_change(worktree_root: Path, branch: str) -> tuple[list[str], str]:
    """Return ``(files, diff_text)`` for what merging ``branch`` would bring in.

    Both are computed against the merge base with the checked-out branch, so
    the score describes this merge rather than the branch's whole history.

    The file list comes from :func:`_incoming_files` and carries its
    ``--no-renames`` reading. The diff body keeps rename detection: it is
    scored as text rather than judged as paths, and inflating a pure rename
    into a delete-plus-add there would change what a blast radius means
    without closing anything.

    Raises:
        IncomingChangeUnreadable: Either read failed. Both halves are scored,
            so a body that did not come out understates the change the same
            way a missing file list does.
    """
    from bernstein.core.git_ops import run_git

    files = _incoming_files(worktree_root, branch)
    spec = f"HEAD...{branch}"
    try:
        body = run_git(["diff", spec], worktree_root, timeout=60)
    except (OSError, subprocess.SubprocessError) as exc:
        msg = f"could not be read: git diff {spec} did not complete ({exc})"
        raise IncomingChangeUnreadable(msg) from exc
    if body.returncode != 0:
        msg = f"could not be read: git diff {spec} exited {body.returncode} ({_sanitise_for_log(body.stderr.strip())})"
        raise IncomingChangeUnreadable(msg)
    return files, body.stdout


def _blast_radius_refusal(
    session: AgentSession,
    worktree_root: Path,
    branch: str,
) -> MergeResult | None:
    """Refuse the merge when it exceeds the operator's blast-radius ceiling.

    Returns ``None`` when the merge may proceed, which includes the default
    case where no ceiling was requested. The ceiling is checked first, so a
    run that never asked for one is not gated by anything here -- including
    by a change that could not be read.

    A change that could not be read refuses. Scoring needs the change, and
    the empty one a failed read would otherwise supply scores as the safest
    possible merge, so an operator who set a ceiling would watch an unjudged
    change land under it.
    """
    from bernstein.core.lifecycle.blast_radius_gate import ceiling_requested, evaluate_pre_merge

    if not ceiling_requested():
        return None

    try:
        files, diff_text = _incoming_change(worktree_root, branch)
    except IncomingChangeUnreadable as exc:
        return _refuse_merge(
            session,
            worktree_root,
            branch,
            reason=f"refused: the change {branch!r} would bring in {exc}",
            code="blast-radius-unreadable",
        )
    decision = evaluate_pre_merge(files=files, diff_text=diff_text)
    if decision is None or decision.allowed:
        return None

    return _refuse_merge(
        session,
        worktree_root,
        branch,
        reason=decision.reason,
        code="blast-radius-ceiling",
    )


# ---------------------------------------------------------------------------
# Signed file-scope gate (issue #3914, decided in #3781)
# ---------------------------------------------------------------------------


class _UnreadableScope:
    """A scope that exists for the session but cannot be read back.

    Distinct from ``None``, which is "no identity record was ever written".
    Both leave the gate with no patterns to match against, and both have to
    land on opposite sides of it: an absent record is the settled open
    default, while a record someone wrote and the store cannot parse is the
    same failure :func:`~bernstein.core.path_scope.paths_outside_scope`
    already answers by admitting nothing. An unreadable scope must not widen
    into "no scope", whether what is unreadable is one pattern or the whole
    record carrying it.
    """

    __slots__ = ()


_UNREADABLE_SCOPE: Final = _UnreadableScope()


def _signed_file_scope(worktree_root: Path, session_id: str) -> list[str] | _UnreadableScope | None:
    """Return the ``allowed_files`` scope signed for ``session_id``.

    ``None`` means no scope was ever declared, which is not the same as an
    empty one: an empty list is a credential that deliberately restricts
    nothing, while ``None`` is the absence of a credential at all. Both are
    unrestricted here, but only the first is a statement someone made.
    :data:`_UNREADABLE_SCOPE` is neither, and refuses.

    The store is opened only when its directory already exists. Constructing
    an :class:`AgentIdentityStore` mints a JWT secret as a side effect, and a
    merge that reads a scope must not be the thing that creates one.
    """
    auth_dir = worktree_root / ".sdd" / "auth"
    if not auth_dir.exists():
        return None

    from bernstein.core.agents.agent_identity import AgentIdentityStore

    try:
        identity = AgentIdentityStore(auth_dir).get(session_id)
    except OSError as exc:
        logger.error("Could not read agent identities for %s: %s", _sanitise_for_log(session_id), exc)
        return _UNREADABLE_SCOPE
    if identity is not None:
        return identity.allowed_files

    # ``get`` answers ``None`` for a session that never had an identity and
    # for one whose record is on disk but does not deserialise -- a file
    # truncated by a crash mid-write, or one whose two copies of the scope
    # disagree, is skipped by the store's shared reader exactly like a record
    # that is not there. An unreadable directory likewise makes every record
    # look absent. Only genuine absence is the settled open default, so the
    # directory is read rather than the difference guessed at.
    try:
        present = any(entry.name == f"{session_id}.json" for entry in (auth_dir / "agent_identities").iterdir())
    except OSError as exc:
        logger.error("Could not list agent identities for %s: %s", _sanitise_for_log(session_id), exc)
        return _UNREADABLE_SCOPE
    return _UNREADABLE_SCOPE if present else None


def _refuse_merge(
    session: AgentSession,
    worktree_root: Path,
    branch: str,
    *,
    reason: str,
    code: str,
) -> MergeResult:
    """Record, log, and score one file-scope refusal.

    Shared by the gate's two refusals so a scope that is out of bounds and a
    scope that cannot be read leave the same evidence behind: an operator
    reconstructing either from ``refused_merges.jsonl`` should not find one
    of them better recorded than the other.
    """
    _record_merge_refusal(worktree_root, session.id, branch, reason=code)
    logger.error(
        "Refusing to merge agent work from %s: %s",
        _sanitise_for_log(session.id),
        _sanitise_for_log(reason),
    )
    from bernstein.core.metric_collector import get_collector

    for task_id in session.task_ids:
        get_collector().record_merge_result(task_id, success=False)
    return MergeResult(success=False, conflicting_files=[], error=reason)


def _file_scope_refusal(
    session: AgentSession,
    worktree_root: Path,
    branch: str,
) -> MergeResult | None:
    """Refuse the merge when it brings in files outside the signed scope.

    Returns ``None`` when the merge may proceed, which includes the two
    default cases: a session with no identity record, and an identity whose
    scope is empty. Every identity minted before this gate existed carries an
    empty scope, so the gate is inert until an operator sets one.

    A record that is on disk and does not resolve is neither of those, and
    refuses: it is a scope someone declared, and the gate cannot see what it
    said. A file list that could not be read refuses for the same reason one
    step further along: the scope is legible and the change is not, and the
    empty list a failed read would supply says "nothing fell outside" in the
    same words a merge that touches nothing says it.

    Only the file list is read here. The diff body is what a blast radius is
    scored from; this gate judges paths, and a body that did not come out
    tells it nothing it needs.

    The refusal is containment rather than prevention. The out-of-scope write
    already happened inside the agent's own worktree; what this stops is that
    change reaching the repository, and the branch is left intact so an
    operator can see what was refused.
    """
    from bernstein.core.path_scope import paths_outside_scope

    patterns = _signed_file_scope(worktree_root, session.id)
    if isinstance(patterns, _UnreadableScope):
        return _refuse_merge(
            session,
            worktree_root,
            branch,
            reason=(
                f"refused: identity {session.id!r} has a signed file scope on disk that "
                f"cannot be read back; an unreadable scope is not an absent one"
            ),
            code="allowed-files-unreadable",
        )
    if not patterns:
        return None

    try:
        files = _incoming_files(worktree_root, branch)
    except IncomingChangeUnreadable as exc:
        return _refuse_merge(
            session,
            worktree_root,
            branch,
            reason=(
                f"refused: identity {session.id!r} is scoped to {', '.join(patterns)}, "
                f"but the file list this merge would bring in {exc}"
            ),
            code="allowed-files-diff-unreadable",
        )
    outside = paths_outside_scope(files, patterns)
    if not outside:
        return None

    return _refuse_merge(
        session,
        worktree_root,
        branch,
        reason=(
            f"refused: identity {session.id!r} is scoped to {', '.join(patterns)}, "
            f"but the merge would bring in {', '.join(outside)}"
        ),
        code="allowed-files-scope",
    )


def _resolve_quality_gate_config(worktree_root: Path) -> QualityGatesConfig | None:
    """Resolve quality gates configuration from seed file if present."""
    seed_file = worktree_root / "bernstein.yaml"
    if seed_file.exists():
        try:
            from bernstein.core.config.seed_parser import parse_seed

            seed = parse_seed(seed_file)
            return seed.quality_gates
        except Exception as exc:
            logger.debug("Failed to parse quality_gates from %s: %s", seed_file, exc)
    return None


def _quality_gate_refusal(
    session: AgentSession,
    worktree_root: Path,
    branch: str,
    *,
    quality_gate_config: QualityGatesConfig | None = None,
) -> MergeResult | None:
    """Run quality gates on the agent's worktree before merging into base branch (#4393).

    If quality gates are enabled, runs all configured gates on the still-alive
    worktree before the merge lands. A blocking gate failure or execution error
    leaves the agent branch unmerged.
    """
    config = quality_gate_config
    if config is None:
        config = _resolve_quality_gate_config(worktree_root)

    if config is None or not config.enabled:
        return None

    from bernstein.core.models import Task
    from bernstein.core.quality.quality_gates import run_quality_gates

    worktree_path = worktree_root / ".sdd" / "worktrees" / session.id
    run_dir = worktree_path if worktree_path.exists() else worktree_root

    task_ids = session.task_ids or [session.id]
    for task_id in task_ids:
        surrogate_task = Task(
            id=task_id,
            role=getattr(session, "role", "backend") or "backend",
            title=getattr(session, "task_title", "") or task_id,
            description="",
        )
        try:
            qg_result = run_quality_gates(surrogate_task, run_dir, worktree_root, config)
        except Exception as exc:
            logger.warning("Quality gates execution failed for task %s in %s: %s", task_id, run_dir, exc)
            return _refuse_merge(
                session,
                worktree_root,
                branch,
                reason=f"refused: quality gates execution errored for task {task_id}: {exc}",
                code="quality-gates-errored",
            )

        if not qg_result.passed:
            failed_gates = [f"quality_gate:{r.gate}" for r in qg_result.gate_results if r.blocked and not r.passed]
            if not failed_gates:
                failed_gates = ["quality_gate:failed"]
            return _refuse_merge(
                session,
                worktree_root,
                branch,
                reason=f"refused: quality gates blocked merge for task {task_id}: {', '.join(failed_gates)}",
                code="quality-gates-blocked",
            )

    return None


# ---------------------------------------------------------------------------
# Merge and worktree branch merge
# ---------------------------------------------------------------------------


def _record_landed_provenance(
    session_id: str,
    worktree_root: Path,
    before_sha: str,
    run_id: str,
) -> None:
    """Record a lineage row per path this merge landed (issue #2789).

    A CLI adapter's subprocess writes never reach ``record_artifact_write``,
    so without this the run's spine holds only its own journal seal: a chain
    that verifies while recording nothing the agent produced.

    Failure is loud but never propagates. The merge is already in git and
    every row is re-derivable from the merge commit, so a provenance write
    that fails must not undo work that landed -- the same reasoning
    ``emit_production_event`` applies one level down.
    """
    if not run_id or not before_sha:
        logger.debug("merge provenance: no run id or base for %s; nothing recorded", _sanitise_for_log(session_id))
        return
    try:
        from bernstein.core.git.git_basic import run_git
        from bernstein.core.lineage.merge_provenance import record_merge_artifacts
        from bernstein.core.security.audit import load_or_create_audit_key

        head = run_git(["rev-parse", "HEAD"], worktree_root, timeout=10)
        if head.returncode != 0:
            logger.warning("merge provenance: could not resolve HEAD after merge for %s", _sanitise_for_log(session_id))
            return
        after_sha = head.stdout.strip()
        if after_sha == before_sha:
            return
        result = record_merge_artifacts(
            worktree_root=worktree_root,
            before_sha=before_sha,
            after_sha=after_sha,
            actor=f"agent/{session_id}",
            lineage_root=worktree_root / ".sdd" / "lineage",
            run_id=run_id,
            hmac_key=load_or_create_audit_key(),
        )
        logger.info(
            "merge provenance: recorded %d/%d landed path(s) for %s at %s",
            len(result.recorded),
            result.total_seen,
            _sanitise_for_log(session_id),
            after_sha[:12],
        )
    except Exception as exc:
        logger.warning(
            "merge provenance: no rows recorded for %s: %s",
            _sanitise_for_log(session_id),
            _sanitise_for_log(str(exc)),
        )


def _run_merge_and_push(
    session: AgentSession,
    worktree_root: Path,
    merge_worktree_branch_fn: Any,
    quality_gate_config: QualityGatesConfig | None = None,
    run_id: str = "",
) -> MergeResult | None:
    """Run the merge subprocess, record metrics, and push on success.

    Caller must already hold whatever serialisation primitive is in use
    (:class:`MergeQueue` submit context or a per-repo lock).  Kept as a
    private helper so the two entry points below stay thin.
    """
    from bernstein.core.git_ops import (
        current_branch,
        protected_default_branches,
        resolve_default_branch,
        safe_push,
    )

    # Safety guardrail: agent work is merged into whatever branch is checked
    # out at ``worktree_root``. If that target IS one of the repository's
    # protected (default trunk) branches, refuse the merge instead of silently
    # landing unreviewed commits on the trunk. ``protected_default_branches``
    # fails closed on an ambiguous default (``origin/HEAD`` unset AND both a
    # local ``main`` and ``master`` present), returning BOTH names so the guard
    # refuses either. An explicit opt-in env override lets operators who really
    # want this proceed.
    target_branch = current_branch(worktree_root)
    protected = protected_default_branches(worktree_root)
    if target_branch is not None and target_branch in protected and not _allow_merge_to_default_branch():
        _record_merge_refusal(worktree_root, session.id, target_branch)
        logger.error(
            "Refusing to merge agent work from %s onto default branch %r: agent "
            "output must not reach a protected trunk without an explicit target. "
            "Check out a non-default branch, or set %s=1 to override.",
            _sanitise_for_log(session.id),
            _sanitise_for_log(target_branch),
            ENV_ALLOW_MERGE_TO_DEFAULT_BRANCH,
        )
        from bernstein.core.metric_collector import get_collector

        for task_id in session.task_ids:
            get_collector().record_merge_result(task_id, success=False)
        return MergeResult(
            success=False,
            conflicting_files=[],
            error=(
                f"refused: target branch {target_branch!r} is the repository "
                f"default branch; set {ENV_ALLOW_MERGE_TO_DEFAULT_BRANCH}=1 to override"
            ),
        )

    # Reversibility gate (#1322): an operator who set ``--max-blast-radius``
    # gets the ceiling evaluated here, on the change this merge would land.
    # Off by default; a run without the flag reaches the merge unchanged.
    blast_radius_refusal = _blast_radius_refusal(session, worktree_root, f"agent/{session.id}")
    if blast_radius_refusal is not None:
        return blast_radius_refusal

    # Signed file scope (#3914): an operator who scoped the agent's credential
    # to a set of paths gets that scope enforced here, where the change is
    # accepted into the repository. Inert for an identity with an empty scope,
    # which is every identity minted before this gate existed.
    file_scope_refusal = _file_scope_refusal(session, worktree_root, f"agent/{session.id}")
    if file_scope_refusal is not None:
        return file_scope_refusal

    # Quality gates (#4393): run quality gates on the still-alive worktree
    # before landing the merge commit. A failing gate refuses the merge and
    # leaves the agent branch unmerged.
    quality_gate_refusal = _quality_gate_refusal(
        session,
        worktree_root,
        f"agent/{session.id}",
        quality_gate_config=quality_gate_config,
    )
    if quality_gate_refusal is not None:
        return quality_gate_refusal

    merge_start = time.perf_counter()
    # Provenance: record the call site before invoking the merge so the
    # log line identifies the WORKER branch + WORKTREE root that triggered
    # this merge attempt (defect 28: every merge commit must have
    # provenance -- a decoy with no provenance must be impossible).
    # Use ``getattr`` so callers passing a minimal stub session
    # (e.g. ``_Stub`` in test_spawner_merge_queue_wiring) don't crash on
    # a missing ``role`` attribute -- the log line must NEVER raise.
    author_role = getattr(session, "role", "<unknown>")
    logger.info(
        "merge_preflight: from=<worktree=%s> to=<main=%s> branch=agent/%s author=<spawner:%s> reason=<reap-and-merge>",
        worktree_root,
        worktree_root,
        session.id,
        author_role,
    )
    # Base for the landed-provenance diff. Read before the merge because
    # ``before..after`` records a fast-forward -- which leaves no merge
    # commit to diff against a parent -- the same way as a true merge.
    from bernstein.core.git.git_basic import run_git as _run_git

    # Reading the base must not decide whether the merge is attempted. A
    # missing or unreadable worktree makes this raise before the merge
    # function is ever called -- ``run_git`` cannot chdir into a path that is
    # not there -- which would turn a provenance aid into a merge gate and
    # take the error away from the merge function that reports it properly.
    # An empty base makes the later recording a documented no-op.
    try:
        _base = _run_git(["rev-parse", "HEAD"], worktree_root, timeout=10)
        before_sha = _base.stdout.strip() if _base.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError) as exc:
        logger.debug("merge provenance: base unreadable at %s: %s", worktree_root, exc)
        before_sha = ""

    merge_result = merge_worktree_branch_fn(session.id, repo_root=worktree_root)
    merge_duration.observe(time.perf_counter() - merge_start)

    from bernstein.core.metric_collector import get_collector

    merge_ok = merge_result is not None and merge_result.success
    for task_id in session.task_ids:
        get_collector().record_merge_result(task_id, success=merge_ok)

    if merge_result and merge_result.success:
        _record_landed_provenance(session.id, worktree_root, before_sha, run_id)
        # Push the branch we actually merged into (never a hard-coded "main").
        # ``target_branch`` is None only on detached HEAD; fall back to the
        # resolved default there.
        push_branch = target_branch or resolve_default_branch(worktree_root)
        push_result = safe_push(worktree_root, push_branch)
        if push_result.ok:
            logger.info("Pushed merged work from %s to origin/%s", session.id, push_branch)
        else:
            logger.warning("Push failed after merge for %s: %s", session.id, push_result.stderr)

    return merge_result


def _do_merge(
    session: AgentSession,
    worktree_root: Path,
    merge_locks: dict[Path, threading.Lock],
    merge_worktree_branch_fn: Any,
    merge_queue: MergeQueue | None = None,
    quality_gate_config: QualityGatesConfig | None = None,
    run_id: str = "",
) -> MergeResult | None:
    """Execute the merge under a lock, record metrics, and push.

    When ``merge_queue`` is provided, the job is enqueued and processed in
    strict FIFO order under :attr:`MergeQueue.merge_lock` -- this fixes
    where the queue was instantiated but never fed. The legacy
    per-repo ``merge_locks`` path is kept as a fallback for callers (and
    tests) that don't plumb a queue through.

    Args:
        session: Agent session being merged.
        worktree_root: Root of the repo/worktree.
        merge_locks: Lock map keyed by repo root (fallback path only).
        merge_worktree_branch_fn: Callable(session_id, repo_root) -> MergeResult.
        merge_queue: Optional :class:`MergeQueue` for serialized FIFO merges
            across concurrent agents.  When None, falls back to the
            per-repo lock.
        quality_gate_config: Optional :class:`QualityGatesConfig` evaluated
            before merge (#4393).

    Returns:
        The MergeResult.
    """
    if merge_queue is not None:
        task_id = session.task_ids[0] if session.task_ids else ""
        task_title = getattr(session, "task_title", "") or ""
        with merge_queue.submit(session.id, task_id=task_id, task_title=task_title):
            return _run_merge_and_push(
                session,
                worktree_root,
                merge_worktree_branch_fn,
                quality_gate_config=quality_gate_config,
                run_id=run_id,
            )

    merge_lock = merge_locks.setdefault(worktree_root, threading.Lock())
    with merge_lock:
        return _run_merge_and_push(
            session,
            worktree_root,
            merge_worktree_branch_fn,
            quality_gate_config=quality_gate_config,
            run_id=run_id,
        )


def _do_cleanup(
    session_id: str,
    worktree_mgr: WorktreeManager,
    warm_pool_entries: dict[str, PoolSlot],
    warm_pool: WarmPool | None,
) -> None:
    """Release the warm pool slot or clean up the worktree."""
    warm_entry = warm_pool_entries.pop(session_id, None)
    if warm_entry is not None and warm_pool is not None:
        warm_pool.release_slot(warm_entry.slot_id)
    else:
        worktree_mgr.cleanup(session_id)


def merge_and_cleanup_worktree(
    session: AgentSession,
    skip_merge: bool,
    *,
    defer_cleanup: bool = False,
    worktree_paths: dict[str, Path],
    worktree_roots: dict[str, Path],
    worktree_managers: dict[Path, WorktreeManager],
    merge_locks: dict[Path, threading.Lock],
    warm_pool_entries: dict[str, PoolSlot],
    warm_pool: WarmPool | None,
    workdir: Path,
    merge_worktree_branch_fn: Any,
    merge_queue: MergeQueue | None = None,
    quality_gate_config: QualityGatesConfig | None = None,
    run_id: str = "",
) -> MergeResult | None:
    """Merge worktree branch back and optionally clean up.

    Args:
        session: The agent session whose worktree to process.
        skip_merge: When True, skip the merge step.
        defer_cleanup: When True, skip worktree cleanup so the caller
            can inspect the merge result and clean up later via
            ``cleanup_worktree``.  Used by task_lifecycle to ensure
            the worktree survives until after PR creation and merge
            verification (BUG-4 fix).
        worktree_paths: Mutable map of session_id -> worktree path.
        worktree_roots: Mutable map of session_id -> repo root.
        worktree_managers: Map of repo root -> WorktreeManager.
        merge_locks: Mutable map of repo root -> Lock.
        warm_pool_entries: Mutable map of session_id -> PoolSlot.
        warm_pool: Optional warm pool.
        workdir: Project working directory.
        merge_worktree_branch_fn: Callable(session_id, repo_root) -> MergeResult.
        merge_queue: Optional :class:`MergeQueue` to serialize concurrent
            merges through a FIFO queue.  Preferred over the ad-hoc
            ``merge_locks`` dict; when None the legacy per-repo lock path
            is used (preserves single-agent behaviour and test harnesses).
        quality_gate_config: Optional :class:`QualityGatesConfig` evaluated
            before merge (#4393).

    Returns:
        MergeResult when worktrees are enabled and skip_merge is False
        (None otherwise).
    """
    if defer_cleanup:
        worktree_path = worktree_paths.get(session.id)
        worktree_root = worktree_roots.get(session.id, workdir.resolve())
    else:
        worktree_path = worktree_paths.pop(session.id, None)
        worktree_root = worktree_roots.pop(session.id, workdir.resolve())
    worktree_mgr = worktree_managers.get(worktree_root)

    if worktree_path is None or worktree_mgr is None:
        return None

    merge_result: MergeResult | None = None
    if not skip_merge:
        merge_result = _do_merge(
            session,
            worktree_root,
            merge_locks,
            merge_worktree_branch_fn,
            merge_queue=merge_queue,
            quality_gate_config=quality_gate_config,
            run_id=run_id,
        )

    if not defer_cleanup:
        _do_cleanup(session.id, worktree_mgr, warm_pool_entries, warm_pool)

    return merge_result


# ---------------------------------------------------------------------------
# Pending push retry queue
# ---------------------------------------------------------------------------


def pending_pushes_path(workdir: Path) -> Path:
    """Return the path to the pending-pushes JSONL file."""
    return workdir / ".sdd" / "runtime" / "pending_pushes.jsonl"


def record_pending_push(
    workdir: Path,
    session_id: str,
    branch: str,
    repo_root: Path,
) -> None:
    """Append a failed push to the retry queue on disk."""
    path = pending_pushes_path(workdir)
    path.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "session_id": session_id,
        "branch": branch,
        "repo_root": str(repo_root),
        "ts": time.time(),
    }
    try:
        with path.open("a") as fh:
            fh.write(json.dumps(entry) + "\n")
        logger.info("Queued pending push for %s (%s)", session_id, repo_root)
    except OSError as exc:
        logger.error("Failed to write pending push for %s: %s", session_id, exc)


def validate_pending_push_entry(
    line: str,
    safe_base: Path,
) -> tuple[Path, str, str] | None:
    """Parse and validate a single pending-push entry line.

    Returns:
        ``(repo_root, branch, session_id)`` on success, or ``None``
        if the entry is invalid or should be skipped.
    """
    line = line.strip()
    if not line:
        return None
    try:
        entry = json.loads(line)
    except json.JSONDecodeError:
        return None
    if not isinstance(entry, dict):
        return None

    raw_repo_root = entry.get("repo_root")
    if not isinstance(raw_repo_root, str):
        return None
    try:
        candidate_root = Path(raw_repo_root).resolve(strict=True)
    except (OSError, RuntimeError):
        return None
    try:
        relative_root = candidate_root.relative_to(safe_base)
    except ValueError:
        logger.warning(
            "Skipping pending push entry: repo_root %r escapes workspace",
            _sanitise_for_log(raw_repo_root),
        )
        return None

    repo_root = (safe_base / relative_root).resolve()
    if not (repo_root / ".git").exists():
        return None

    branch = entry.get("branch", "main")
    if not isinstance(branch, str):
        branch = "main"
    session_id = entry.get("session_id", "unknown")
    if not isinstance(session_id, str):
        session_id = "unknown"
    return repo_root, branch, session_id


def retry_pending_pushes(workdir: Path) -> int:
    """Retry any pushes recorded in the pending-pushes file.

    Successfully pushed entries are removed from the file.  Entries
    that still fail are kept for the next tick.

    Returns:
        Number of pushes successfully retried.
    """
    path = pending_pushes_path(workdir)
    if not path.exists():
        return 0

    try:
        lines = path.read_text().splitlines()
    except OSError:
        return 0

    if not lines:
        return 0

    from bernstein.core.git_ops import safe_push

    remaining: list[str] = []
    retried = 0

    safe_base = pending_pushes_path(workdir).resolve().parent.parent.parent
    for line in lines:
        validated = validate_pending_push_entry(line, safe_base)
        if validated is None:
            continue
        repo_root, branch, session_id = validated

        safe_session_id = _sanitise_for_log(session_id)
        safe_repo_root = _sanitise_for_log(str(repo_root))
        push_result = safe_push(repo_root, branch)
        if push_result.ok:
            logger.info(
                "Retry push succeeded for %s (%s)",
                safe_session_id,
                safe_repo_root,
            )
            retried += 1
        else:
            logger.warning(
                "Retry push still failing for %s: %s",
                safe_session_id,
                _sanitise_for_log(push_result.stderr),
            )
            remaining.append(line)

    # Rewrite file with only the entries that still failed
    try:
        if remaining:
            path.write_text("\n".join(remaining) + "\n")
        else:
            path.unlink(missing_ok=True)
    except OSError as exc:
        logger.warning("Failed to update pending pushes file: %s", exc)

    return retried


# ---------------------------------------------------------------------------
# Trace finalization
# ---------------------------------------------------------------------------


def finalize_agent_trace(
    session: AgentSession,
    traces: dict[str, AgentTrace],
    trace_store: TraceStore,
) -> None:
    """Write the finalized trace for a reaped session."""
    trace = traces.pop(session.id, None)
    if trace is not None:
        outcome = "success" if session.status != "dead" else "unknown"
        finalize_trace(trace, outcome)
        try:
            trace_store.write(trace)
        except Exception as exc:
            logger.warning("Failed to write finalized trace for %s: %s", session.id, exc)


def update_trace_outcome(
    session_id: str,
    outcome: str,
    traces: dict[str, AgentTrace],
    trace_store: TraceStore,
) -> None:
    """Update the stored trace outcome for a session.

    Called by the orchestrator when it learns a task succeeded or failed
    via the task server (before the process is reaped).

    Args:
        session_id: The session whose trace should be updated.
        outcome: "success" or "failed".
        traces: Mutable traces dict.
        trace_store: TraceStore for persistence.
    """
    trace = traces.get(session_id)
    if trace is None:
        return
    if outcome in ("success", "failed", "unknown"):
        trace.outcome = outcome  # type: ignore[assignment]
        try:
            trace_store.write(trace)
        except Exception as exc:
            logger.warning("Failed to update trace outcome for %s: %s", session_id, exc)


# ---------------------------------------------------------------------------
# Merge worktree branch
# ---------------------------------------------------------------------------


def merge_worktree_branch(
    session_id: str,
    workdir: Path,
    repo_root: Path | None = None,
) -> MergeResult:
    """Merge the agent's worktree branch with conflict detection.

    Uses ``merge_with_conflict_detection`` for a safe, abort-on-conflict
    merge.  On success the branch is merged; on conflict the merge is
    aborted and the caller receives the list of conflicting files.

    Args:
        session_id: The session whose branch should be merged.
        workdir: Project working directory (fallback merge root).
        repo_root: Optional explicit repo root for the merge.

    Returns:
        MergeResult with success status and any conflicting files.
    """
    branch_name = f"agent/{session_id}"
    merge_root = (repo_root or workdir).resolve()
    try:
        result = merge_with_conflict_detection(
            merge_root,
            branch_name,
            message=f"Merge {branch_name}",
        )
        if result.success:
            logger.info("Merged worktree branch %s into current branch", branch_name)
        elif result.conflicting_files:
            logger.warning(
                "Merge conflicts for %s in files: %s",
                session_id,
                ", ".join(result.conflicting_files),
            )
        else:
            # issue #2792: a non-conflict merge-back failure discards committed
            # work and blocks the task; it is an error-level event, not a soft
            # warning, so a run that is silently losing output is visible.
            logger.error("Merge failed for %s: %s", session_id, result.error)
        return result
    except Exception as exc:
        logger.error("Merge failed for %s: %s", session_id, exc)
        return MergeResult(success=False, conflicting_files=[], error=str(exc))


# ---------------------------------------------------------------------------
# Reap helpers
# ---------------------------------------------------------------------------


def reap_openclaw(
    session: AgentSession,
    runtime_bridge: Any,
    run_bridge_call_fn: Any,
) -> None:
    """Sync logs from the remote bridge for an OpenClaw session."""
    from bernstein.bridges.base import BridgeError

    if runtime_bridge is not None:
        try:
            run_bridge_call_fn(runtime_bridge.logs(session.id))
        except BridgeError as exc:
            logger.warning("OpenClaw log sync failed for %s: %s", session.id, exc)
    logger.info("Agent %s remote bridge run finalized", session.id)


def reap_container(
    session: AgentSession,
    container_mgr: ContainerManager | None,
    sandbox_managers: dict[str, ContainerManager],
) -> None:
    """Destroy the container for a containerized agent session."""
    mgr: ContainerManager | None = sandbox_managers.get(session.id, container_mgr)
    if session.container_id and mgr is not None:
        handle = mgr.get_handle(session.id)
        if handle is not None:
            mgr.destroy(handle)
        sandbox_managers.pop(session.id, None)
        logger.info("Agent %s container destroyed", session.id)


def reap_in_process(
    session: AgentSession,
    in_process: InProcessAgent | None,
    backend: AgentBackend,
) -> bool:
    """Wait on and clean up an in-process agent. Returns True if reaped."""
    if in_process is None or backend != AgentBackend.IN_PROCESS:
        return False
    exit_code_val = in_process.wait(session.id, timeout=5.0)
    if exit_code_val is not None:
        session.exit_code = exit_code_val
    in_process.cleanup(session.id)
    logger.info("Agent %s in-process agent cleaned up", session.id)
    return True


def reap_subprocess(
    session: AgentSession,
    procs: dict[str, subprocess.Popen[bytes] | None],
) -> None:
    """Terminate and wait on the OS subprocess."""
    proc = procs.pop(session.id, None)
    if proc is not None:
        try:
            proc.terminate()
        except Exception as exc:
            logger.warning("reap_completed_agent: terminate failed for %s: %s", session.id, exc)
        try:
            session.exit_code = proc.wait(timeout=5)
        except Exception as exc:
            logger.warning("reap_completed_agent: wait failed for %s: %s", session.id, exc)
    logger.info("Agent %s process reaped", session.id)


def reap_completed_agent(
    session: AgentSession,
    *,
    skip_merge: bool = False,
    defer_cleanup: bool = False,
    # --- Dependencies (from spawner state) ---
    runtime_bridge: Any,
    run_bridge_call_fn: Any,
    container_mgr: ContainerManager | None,
    sandbox_managers: dict[str, ContainerManager],
    in_process: InProcessAgent | None,
    backend: AgentBackend,
    procs: dict[str, subprocess.Popen[bytes] | None],
    worktree_paths: dict[str, Path],
    worktree_roots: dict[str, Path],
    worktree_managers: dict[Path, WorktreeManager],
    merge_locks: dict[Path, threading.Lock],
    warm_pool_entries: dict[str, PoolSlot],
    warm_pool: WarmPool | None,
    workdir: Path,
    merge_worktree_branch_fn: Any,
    traces: dict[str, AgentTrace],
    trace_store: TraceStore,
    merge_queue: MergeQueue | None = None,
    quality_gate_config: QualityGatesConfig | None = None,
) -> MergeResult | None:
    """Terminate and wait on the subprocess for a completed agent.

    Calls proc.terminate() then proc.wait(timeout=5) to reap the OS
    process.  Handles bridge, container, in-process, and subprocess agents.

    Args:
        session: The AgentSession whose underlying process should be reaped.
        skip_merge: When True, skip the worktree merge.
        defer_cleanup: When True, keep the worktree alive after merge.
        (remaining args are spawner state passed through)

    Returns:
        MergeResult when worktrees are enabled and skip_merge is False
        (None otherwise).
    """
    from bernstein.core.agents.agent_ipc import unregister_stdin_pipe

    unregister_stdin_pipe(session.id)

    if session.runtime_backend == "openclaw":
        reap_openclaw(session, runtime_bridge, run_bridge_call_fn)
    else:
        reap_container(session, container_mgr, sandbox_managers)

        if reap_in_process(session, in_process, backend):
            worktree_paths.pop(session.id, None)
            worktree_roots.pop(session.id, None)
        else:
            reap_subprocess(session, procs)
            merge_result = merge_and_cleanup_worktree(
                session,
                skip_merge,
                defer_cleanup=defer_cleanup,
                worktree_paths=worktree_paths,
                worktree_roots=worktree_roots,
                worktree_managers=worktree_managers,
                merge_locks=merge_locks,
                warm_pool_entries=warm_pool_entries,
                warm_pool=warm_pool,
                workdir=workdir,
                merge_worktree_branch_fn=merge_worktree_branch_fn,
                merge_queue=merge_queue,
                quality_gate_config=quality_gate_config,
            )
            outcome = "completed" if session.status != "dead" else "timed_out"
            get_plugin_manager().fire_agent_reaped(session_id=session.id, role=session.role, outcome=outcome)
            return merge_result

    finalize_agent_trace(session, traces, trace_store)
    merge_result = merge_and_cleanup_worktree(
        session,
        skip_merge,
        defer_cleanup=defer_cleanup,
        worktree_paths=worktree_paths,
        worktree_roots=worktree_roots,
        worktree_managers=worktree_managers,
        merge_locks=merge_locks,
        warm_pool_entries=warm_pool_entries,
        warm_pool=warm_pool,
        workdir=workdir,
        merge_worktree_branch_fn=merge_worktree_branch_fn,
        merge_queue=merge_queue,
        quality_gate_config=quality_gate_config,
    )
    outcome = "completed" if session.status != "dead" else "timed_out"
    get_plugin_manager().fire_agent_reaped(session_id=session.id, role=session.role, outcome=outcome)
    return merge_result
