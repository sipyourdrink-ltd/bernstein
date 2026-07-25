"""Verify task completion via concrete signals.

The janitor validates completed work against defined completion signals.
For upgrade tasks, it performs additional verification of the upgrade execution.
Supports LLM Judge for ambiguous task verification via Claude Sonnet.
"""

from __future__ import annotations

import glob as globmod
import json
import logging
import os
import re
import subprocess
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

import httpx

from bernstein import _BUNDLED_TEMPLATES_DIR  # type: ignore[reportPrivateUsage]
from bernstein.core.completion_budget import CompletionBudget
from bernstein.core.guardrails import GuardrailsConfig, run_guardrails
from bernstein.core.llm import call_llm
from bernstein.core.models import (
    CompletionSignal,
    GuardrailResult,
    JanitorResult,
    JudgeVerdict,
    Task,
    TaskType,
)

if TYPE_CHECKING:
    from bernstein.core.persistence.lineage import LineageVerificationResult

logger = logging.getLogger(__name__)

# --- Judge constants ---

MAX_JUDGE_RETRIES = 2
# Last-resort fallback constants used only when neither the operator's
# bernstein.yaml (judge_model/judge_provider) nor an explicit caller
# argument supplies a value. Every quality-judge call was previously
# hardcoded to these, billing Claude via OpenRouter regardless of the
# run's configured model -- see judge_task()/run_janitor() for the
# resolution order.
JUDGE_MODEL = "anthropic/claude-sonnet-4-20250514"
JUDGE_PROVIDER = "openrouter"
JUDGE_MAX_DIFF_CHARS = 10_000  # Truncate diff to control cost
JUDGE_MAX_TOKENS = 1024  # Response token limit (~$0.015 output at Sonnet rates)
JUDGE_CONFIDENCE_THRESHOLD = 0.7  # Below this, flag for human review
_JUDGE_RETRY_RE = re.compile(r"\[judge_retry:(\d+)\]")
_JUDGE_TEMPLATE_PATH = _BUNDLED_TEMPLATES_DIR / "prompts" / "judge.md"

# --- Attribution (item 15 + S2 family) ---
#
# The janitor historically judged a task purely by evaluating its completion
# signals against the SHARED repo state. Two failure modes fell out of that
# (proofs/d2/minimax/attempt-e938bd33 + S2 orphan family):
#   1. Misattribution: a 0-file manager task got janitor_passed=true because a
#      CHILD task's landed work satisfied the manager's signals, while the
#      backend task that made the real green commit (b9574d9) was judged
#      against the wrong repo snapshot and failed.
#   2. Rubber-stamped orphans: crash-recovery auto-completions with an empty
#      diff ($0 / 0 tokens, no commits) were accepted because "no failing
#      signal" was treated as "passed".
#
# Fix: before accepting, attribute concrete work to THE TASK BEING JUDGED --
# commits whose message references the task id, falling back to changed files
# in the task's owned_files -- and REJECT any non-no-op task with an empty
# attributed diff. Attribution inputs/outputs are logged on every verdict so
# a future misattribution is a log read, not a proof-archive autopsy.

# Task types that legitimately produce no diff (research/exploration output
# lives in task notes, not the repo). Everything else must show work.
#
# TaskType.UPGRADE_PROPOSAL is included because verify_upgrade_task() (see
# the UPGRADE_PROPOSAL branch above) is the real pass/fail authority for
# these tasks, and UpgradeProposal.to_task() never sets owned_files -- there
# is no attribution source for this task type, so the generic empty-diff
# guard would hard-reject an already-verified upgrade proposal.
_NOOP_TASK_TYPES = frozenset({TaskType.RESEARCH, TaskType.UPGRADE_PROPOSAL})

_ATTRIBUTION_MAX_COMMITS = 50

# Completion-signal types that constitute real evidence a task did work
# (as opposed to a bare path_exists / glob_exists that a rubber-stamp could
# trivially satisfy). A passing signal of one of these types lets the janitor
# accept a task whose diff is not attributable (empty-diff warn path) instead
# of hard-rejecting it.
# The artifact-mode criteria (issue #2608) verify the produced artifact's
# canonical bytes against a declared contract, so a passing one is strong
# evidence real work happened - on par with a passing test.
_NONTRIVIAL_SIGNAL_TYPES = frozenset(
    {
        "test_passes",
        "file_contains",
        "llm_review",
        "llm_judge",
        "schema_valid",
        "criteria_match",
        "hash_stable",
        # A grounded report proves the figures trace to signed sources - strong
        # evidence of real work, on par with a passing test (issue #2888).
        "figures_grounded",
    }
)

#: Completion-signal types that operate on an artifact's canonical bytes rather
#: than the filesystem (issues #2608, #2888). Only
#: :func:`evaluate_artifact_signals`, which has the produced artifact in scope,
#: can pass one; every filesystem-path evaluation of these types fails closed
#: (issue #2968), so a declared gate that no evaluator checked can never read as
#: verified. The evaluator that reaches them in production is the artifact-mode
#: completion path (:mod:`bernstein.core.tasks.artifact_completion`), which
#: loads the artifact and dispatches them here.
ARTIFACT_SIGNAL_TYPES = frozenset({"schema_valid", "criteria_match", "hash_stable", "figures_grounded"})

#: Back-compat alias for the module-private spelling.
_ARTIFACT_SIGNAL_TYPES = ARTIFACT_SIGNAL_TYPES


def _has_nontrivial_passing_signal(task: Task, signal_results: list[tuple[str, bool, str]]) -> bool:
    """True if the task has at least one passing non-trivial completion signal.

    ``signal_results`` entries are ``(desc, passed, detail)`` where ``desc`` is
    ``f"{signal.type}: {signal.value}"`` (see :func:`_collect_signal_results`),
    so the type is the text before the first ``": "``. A passing signal of a
    :data:`_NONTRIVIAL_SIGNAL_TYPES` type is treated as evidence real work
    happened, which lets an unattributable-diff task be accepted with a warning
    rather than hard-rejected.
    """
    for desc, passed, _detail in signal_results:
        if not passed:
            continue
        signal_type = desc.split(":", 1)[0].strip()
        if signal_type in _NONTRIVIAL_SIGNAL_TYPES:
            return True
    return False


def _is_git_repo(workdir: Path) -> bool:
    """True when *workdir* is inside a git work tree (attribution possible)."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            cwd=workdir,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        logger.warning("janitor attribution: git repo detection failed in %s: %s", workdir, exc)
        return False
    return result.returncode == 0 and result.stdout.strip() == "true"


def _attribute_task_work(task: Task, workdir: Path) -> tuple[list[str], list[str], str]:
    """Attribute concrete repo work to *the task being judged*.

    Resolution order (each step logged):
      1. Commits on HEAD whose message contains the task id (fixed-string
         ``git log --grep``) -- the strongest signal that a commit belongs to
         this task. Changed files are the union across those commits.
      2. Fallback: files changed in the last commit, restricted to the
         task's ``owned_files`` when set. This is explicitly weaker (it can
         see another task's landing commit) and the returned reason says so.

    Args:
        task: The task under judgment.
        workdir: Project root (must already be a verified git repo).

    Returns:
        Tuple of (attributed commit shas, attributed changed files, human
        reason describing HOW attribution was decided).
    """
    try:
        log_result = subprocess.run(
            [
                "git",
                "log",
                f"--max-count={_ATTRIBUTION_MAX_COMMITS}",
                "--format=%H",
                "--fixed-strings",
                f"--grep={task.id}",
            ],
            cwd=workdir,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        logger.warning("janitor attribution: git log --grep failed for task=%s: %s", task.id, exc)
        log_result = None

    if log_result is not None and log_result.returncode == 0:
        commits = [line.strip() for line in log_result.stdout.splitlines() if line.strip()]
        if commits:
            files: list[str] = []
            for sha in commits:
                try:
                    show = subprocess.run(
                        ["git", "show", "--name-only", "--format=", sha],
                        cwd=workdir,
                        capture_output=True,
                        text=True,
                        timeout=30,
                    )
                except (subprocess.TimeoutExpired, OSError) as exc:
                    logger.warning("janitor attribution: git show %s failed: %s", sha, exc)
                    continue
                if show.returncode == 0:
                    files.extend(line.strip() for line in show.stdout.splitlines() if line.strip())
            unique_files = sorted(set(files))
            reason = (
                f"commit-message match: {len(commits)} commit(s) reference task id {task.id!r} "
                f"({[c[:12] for c in commits]}), {len(unique_files)} changed file(s)"
            )
            return commits, unique_files, reason

    # Fallback: last-commit diff scoped to owned_files. Weaker attribution --
    # says the repo moved in this task's file territory, not that THIS task
    # moved it. WITHOUT owned_files there is no territory to scope to: an
    # unscoped last-commit diff would attribute ANOTHER task's landing commit
    # to this one (exactly the archived manager rubber-stamp), so we
    # deliberately attribute nothing in that case.
    if not task.owned_files:
        return (
            [],
            [],
            f"no commit references task id {task.id!r} and task has no owned_files -- "
            "nothing attributable to this task (unscoped last-commit diff would "
            "misattribute another task's work)",
        )
    cmd = ["git", "diff", "HEAD~1", "--name-only", "--", *task.owned_files]
    try:
        diff_result = subprocess.run(
            cmd,
            cwd=workdir,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        logger.warning("janitor attribution: fallback diff failed for task=%s: %s", task.id, exc)
        return [], [], f"attribution failed: no task-id commits and fallback diff errored ({exc})"

    files = [line.strip() for line in diff_result.stdout.splitlines() if line.strip()]
    scope = f"owned_files={task.owned_files}" if task.owned_files else "all files (no owned_files)"
    reason = (
        f"fallback last-commit diff over {scope}: {len(files)} changed file(s); "
        f"NO commit references task id {task.id!r} -- weak attribution"
    )
    return [], sorted(set(files)), reason


def evaluate_signal(signal: CompletionSignal, workdir: Path) -> tuple[bool, str]:
    """Evaluate a single completion signal against the filesystem.

    Args:
        signal: The signal to check.
        workdir: Project root -- relative paths resolve against this.

    Returns:
        Tuple of (passed, detail_message).
    """
    match signal.type:
        case "path_exists":
            return _check_path_exists(signal.value, workdir)
        case "glob_exists":
            ok = _check_glob_exists(signal.value, workdir)
            return ok, "matched" if ok else "no matches"
        case "test_passes":
            command = _resolve_branch_check_command(signal.value, workdir)
            ok = _check_test_passes(command, workdir)
            return ok, "exit 0" if ok else "non-zero exit"
        case "file_contains":
            ok = _check_file_contains(signal.value, workdir)
            return ok, "found" if ok else "not found"
        case "llm_review":
            return _check_llm_review(signal.value, workdir)
        case "llm_judge":
            # llm_judge requires async evaluation - use judge_task() instead.
            return False, "llm_judge requires async evaluation via judge_task()"
        case _ if signal.type in _ARTIFACT_SIGNAL_TYPES:
            # Artifact-mode criteria operate on the produced artifact's canonical
            # bytes (and, for figures_grounded, on lineage reads), not the
            # filesystem. They are dispatched by evaluate_artifact_signals() once
            # the artifact is in scope; the filesystem path recognises them (so
            # they are never "unknown") but cannot evaluate them here, and so
            # fails closed. Membership is read from _ARTIFACT_SIGNAL_TYPES rather
            # than re-listed, so adding a type to the set cannot leave this arm
            # behind.
            return False, f"{signal.type} requires artifact-mode evaluation via evaluate_artifact_signals()"
    return False, f"unknown signal type: {signal.type}"


def evaluate_artifact_signals(
    task: Task,
    artifact: object,
    *,
    lineage_root: Path | None = None,
    operator_secret: bytes | None = None,
) -> list[tuple[str, bool, str]]:
    """Evaluate a task's artifact-mode completion signals against ``artifact``.

    ``artifact`` is the raw produced artifact (str / bytes / list / mapping, or
    a :class:`~bernstein.core.tasks.figures.ReportBundle` for a figures report);
    it is canonicalised under the task's declared
    :attr:`~bernstein.core.tasks.models.Task.artifact_spec` kind and each
    ``schema_valid`` / ``criteria_match`` / ``hash_stable`` signal is evaluated
    by its closed evaluator in :mod:`bernstein.core.tasks.artifacts`.

    ``figures_grounded`` (issue #2888) additionally resolves each declared
    figure's anchor against the signed lineage log rooted at ``lineage_root``
    (the project root containing ``.sdd/``). A ``warn``-severity signal
    downgrades a grounding failure to a passing result whose detail is prefixed
    ``WARN:`` so completion is not blocked; the default severity is ``strict``.

    Returns a list of ``(description, passed, detail)`` tuples in the same shape
    as :func:`_collect_signal_results`. Filesystem-oriented signals on the task
    are ignored here (they are evaluated by :func:`verify_task`).
    """
    from bernstein.core.tasks.artifacts import evaluate_criterion

    kind = task.artifact_spec.kind
    results: list[tuple[str, bool, str]] = []
    for signal in task.completion_signals:
        if signal.type not in _ARTIFACT_SIGNAL_TYPES:
            continue
        desc = f"{signal.type}: {signal.value}"
        if signal.type == "figures_grounded":
            passed, detail = _evaluate_figures_grounded_signal(
                artifact, signal.value, lineage_root=lineage_root, operator_secret=operator_secret
            )
        else:
            passed, detail = evaluate_criterion(signal.type, signal.value, artifact=artifact, kind=kind)
        results.append((desc, passed, detail))
    return results


def _parse_figures_severity(value: str) -> str:
    """Return ``"strict"`` or ``"warn"`` for a ``figures_grounded`` signal value.

    An empty value and any unrecognised value fall closed to ``strict`` - the
    default posture is that an unanchored figure fails completion. Only an
    explicit ``warn`` (bare or as ``{"severity": "warn"}``) downgrades.
    """
    text = (value or "").strip()
    if not text:
        return "strict"
    lowered = text.lower()
    if lowered in ("strict", "warn"):
        return lowered
    if text.startswith("{"):
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            return "strict"
        sev = str(payload.get("severity", "strict")).lower()
        return "warn" if sev == "warn" else "strict"
    return "strict"


def _apply_figures_severity(severity: str, passed: bool, detail: str) -> tuple[bool, str]:
    """Downgrade a grounding failure to a passing ``WARN:`` when severity is warn."""
    if not passed and severity == "warn":
        return True, f"WARN: {detail}"
    return passed, detail


def _evaluate_figures_grounded_signal(
    artifact: object,
    value: str,
    *,
    lineage_root: Path | None,
    operator_secret: bytes | None,
) -> tuple[bool, str]:
    """Evaluate ``figures_grounded`` on a report bundle against the lineage log."""
    from bernstein.core.tasks.figures import (
        CanonicalisationError,
        ReportBundle,
        evaluate_figures_grounded,
        parse_report_bundle,
    )

    severity = _parse_figures_severity(value)

    if isinstance(artifact, ReportBundle):
        bundle = artifact
    elif isinstance(artifact, (bytes, bytearray)):
        try:
            bundle = parse_report_bundle(bytes(artifact))
        except CanonicalisationError as exc:
            return _apply_figures_severity(severity, False, f"artifact is not a report bundle: {exc}")
    else:
        return _apply_figures_severity(
            severity, False, "figures_grounded requires a report bundle artifact (body + figures.json)"
        )

    if lineage_root is None:
        return _apply_figures_severity(severity, False, "no lineage root available to resolve figure anchors")

    from bernstein.core.lineage.figure_grounding import LineageAnchorResolver

    sdd = Path(lineage_root) / ".sdd"
    resolver = LineageAnchorResolver(
        log_path=sdd / "lineage" / "log.jsonl",
        cards_dir=sdd / "agents",
        operator_secret=operator_secret,
    )
    verdict = evaluate_figures_grounded(bundle, resolve_anchor=resolver.resolve)
    if verdict.ok:
        return True, f"{len(verdict.provenances)} figure(s) grounded"
    return _apply_figures_severity(severity, False, "; ".join(verdict.failures))


def verify_task(task: Task, workdir: Path) -> tuple[bool, list[str]]:
    """Verify all completion signals for a task.

    Every declared signal is dispatched through :func:`evaluate_signal`, which
    owns the per-type default; this function never decides an outcome of its
    own. Artifact-mode criteria therefore fail closed here with the detail
    ``evaluate_signal`` already produces (issue #2968): the filesystem path
    does not have the produced artifact in scope, and a declared gate that no
    evaluator checked must not report as passed. Only
    :func:`evaluate_artifact_signals`, called with the artifact in scope, can
    pass them.

    Args:
        task: Task with completion_signals to check.
        workdir: Project root for path resolution.

    Returns:
        Tuple of (all_passed, list_of_failed_signal_descriptions). Failed
        descriptions carry the evaluation detail so actionable causes (e.g.
        a gitignore-excluded deliverable the merge-back never staged) reach
        retry/fail surfaces. If no signals defined, returns (True, []).
    """
    failed: list[str] = []
    for signal in task.completion_signals:
        passed, detail = evaluate_signal(signal, workdir)
        if not passed:
            desc = f"{signal.type}: {signal.value}"
            if detail:
                desc = f"{desc} ({detail})"
            failed.append(desc)
    all_passed = len(failed) == 0
    return all_passed, failed


def _collect_signal_results(
    task: Task,
    workdir: Path,
) -> list[tuple[str, bool, str]]:
    """Evaluate all signals and return structured results.

    Shares :func:`evaluate_signal` with :func:`verify_task`, so the two paths
    report the same outcome for the same signal type by construction. The one
    signal type omitted here is ``llm_judge``, which :func:`run_janitor`
    genuinely does evaluate later in the same pass via
    :func:`_evaluate_judge_signals`; artifact-mode signals have no such
    in-pass evaluator and so fail closed rather than being dropped
    (issue #2968).

    Args:
        task: Task with completion_signals to check.
        workdir: Project root for path resolution.

    Returns:
        List of (signal_description, passed, detail) tuples.
    """
    results: list[tuple[str, bool, str]] = []
    for signal in task.completion_signals:
        if signal.type == "llm_judge":
            continue  # Evaluated async in run_janitor via judge_task()
        desc = f"{signal.type}: {signal.value}"
        passed, detail = evaluate_signal(signal, workdir)
        results.append((desc, passed, detail))
    return results


async def _evaluate_judge_signals(
    task: Task,
    workdir: Path,
    signal_results: list[tuple[str, bool, str]],
    *,
    judge_model: str | None = None,
    judge_provider: str | None = None,
) -> JudgeVerdict | None:
    """Evaluate llm_judge signals and append results to signal_results."""
    judge_signals = [s for s in task.completion_signals if s.type == "llm_judge"]
    if not judge_signals:
        return None

    non_judge_ok = all(ok for _, ok, _ in signal_results)
    if not non_judge_ok:
        for js in judge_signals:
            signal_results.append((f"llm_judge: {js.value}", False, "skipped: prerequisite signals failed"))
        return None

    criteria = judge_signals[0].value
    verdict = await judge_task(task, workdir, criteria, judge_model=judge_model, judge_provider=judge_provider)
    judge_desc = f"llm_judge: {criteria}"
    if verdict.verdict == "accept":
        signal_results.append((judge_desc, True, f"accepted (confidence: {verdict.confidence:.2f})"))
    else:
        signal_results.append((judge_desc, False, f"retry: {verdict.feedback}"))
    return verdict


async def _create_fix_tasks_if_needed(
    task: Task,
    all_passed: bool,
    failed_descs: list[str],
    judge_verdict: JudgeVerdict | None,
    server_url: str | None,
    workdir: Path,
) -> list[str]:
    """Create fix tasks when signals fail. Returns list of fix task IDs."""
    if all_passed or server_url is None:
        return []

    if judge_verdict and judge_verdict.verdict == "retry":
        retry_count = _get_judge_retry_count(task)
        if retry_count < MAX_JUDGE_RETRIES:
            return await _create_judge_fix_task(task, judge_verdict, retry_count, server_url, workdir=workdir)
        logger.warning("Task %s exceeded max judge retries (%d), not creating fix task", task.id, MAX_JUDGE_RETRIES)
        return []

    return await create_fix_tasks(task, failed_descs, server_url, workdir=workdir)


def compact_lineage_logs(workdir: Path) -> list[str]:
    """Gzip rotated WAL files so lineage records stay compact.

    Called from the janitor pass alongside task verification. The
    active WAL file is left alone -- only rotated backups
    (``<run_id>.wal.jsonl.<N>``) are compressed in place. Returns the
    list of file names that were compressed.
    """
    from bernstein.core.persistence.lineage import compress_rotated_lineage

    return compress_rotated_lineage(workdir / ".sdd")


def verify_lineage_chains(
    workdir: Path,
    *,
    audit_log: Any = None,
    sink: Any = None,
    verifier: Any = None,
) -> list[LineageVerificationResult]:
    """Re-verify every run's lineage chain and surface tampering loudly.

    The janitor calls this directly after :func:`compact_lineage_logs`
    so a compactor that ran against a tampered file is followed by a
    fresh chain check. Failures emit (a) an entry in ``audit.jsonl`` of
    type ``lineage_tamper_detected``, (b) an increment of
    ``bernstein_lineage_tamper_total{run_id=...}``, (c) a webhook call
    to the configured SIEM sink. Verification failures NEVER raise --
    the operator's response policy lives in the SIEM, not in the
    orchestrator.

    Args:
        workdir: Project root containing ``.sdd``.
        audit_log: Optional :class:`bernstein.core.security.audit.AuditLog`
            instance. When set, a ``lineage_tamper_detected`` event is
            appended for every failed run.
        sink: Optional :class:`LineageAlertSink`. When set, a
            ``LineageTamperEvent`` is delivered for each failed run.
        verifier: Optional :class:`LineageVerifier`. When set, customer
            signatures are checked alongside the WAL hash chain.

    Returns:
        One :class:`LineageVerificationResult` per run inspected.
    """
    from bernstein.core.persistence.lineage import LineageReader, verify_run_chain

    sdd_dir = workdir / ".sdd"
    reader = LineageReader(sdd_dir)
    results: list[LineageVerificationResult] = []
    run_ids = list(reader._iter_run_ids())
    for run_id in run_ids:
        try:
            result = verify_run_chain(sdd_dir, run_id, verifier=verifier)
        except Exception:
            logger.warning("lineage: verify failed for run %s", run_id, exc_info=True)
            continue
        results.append(result)
        if result.ok:
            continue
        _surface_tamper(run_id, result, audit_log=audit_log, sink=sink)
    return results


def _surface_tamper(
    run_id: str,
    result: LineageVerificationResult,
    *,
    audit_log: Any,
    sink: Any,
) -> None:
    """Emit metric + audit event + SIEM alert for a failed verification."""
    import time

    from bernstein.core.observability.lineage_alert import LineageTamperEvent

    try:
        from bernstein.core.observability.prometheus import lineage_tamper_total

        lineage_tamper_total.labels(run_id=run_id).inc()
    except Exception:
        logger.warning("lineage: failed to increment tamper counter", exc_info=True)

    if audit_log is not None:
        try:
            audit_log.log(
                event_type="lineage_tamper_detected",
                actor="janitor",
                resource_type="lineage_run",
                resource_id=run_id,
                details={
                    "run_id": run_id,
                    "errors": result.errors[:20],
                    "error_count": len(result.errors),
                    "record_count": result.record_count,
                },
            )
        except Exception:
            logger.warning("lineage: failed to write tamper audit event", exc_info=True)

    if sink is not None:
        try:
            sink.emit(
                LineageTamperEvent(
                    run_id=run_id,
                    errors=list(result.errors),
                    record_count=result.record_count,
                    detected_at=time.time(),
                )
            )
        except Exception:
            logger.warning("lineage: alert sink raised; broken sink", exc_info=True)

    logger.warning(
        "lineage tamper detected for run=%s errors=%d records=%d",
        run_id,
        len(result.errors),
        result.record_count,
    )


async def run_janitor(
    tasks: list[Task],
    workdir: Path,
    *,
    server_url: str | None = None,
    guardrails_config: GuardrailsConfig | None = None,
    permission_mode: str | None = None,
    judge_model: str | None = None,
    judge_provider: str | None = None,
) -> list[JanitorResult]:
    """Evaluate tasks and return structured results.

    Only considers tasks that have at least one completion signal.
    Tasks with no signals are skipped.

    When server_url is provided and signals fail, auto-creates fix tasks
    via POST /tasks on the server.

    For upgrade proposal tasks, performs additional upgrade verification.

    Args:
        tasks: Tasks to evaluate.
        workdir: Project root for path resolution.
        server_url: Optional task server URL for auto-creating fix tasks.
        guardrails_config: Guardrail configuration. Defaults to GuardrailsConfig()
            (all checks enabled). Pass None to disable all guardrails.
        permission_mode: Permission mode string (bypass/plan/auto/default).
            When ``"bypass"``, non-immune guardrail checks are relaxed.
        judge_model: Optional model override for llm_judge signal evaluation.
            Falls back to the operator's ``bernstein.yaml`` judge_model, then
            to ``JUDGE_MODEL`` as a last resort.
        judge_provider: Optional provider override for llm_judge signal
            evaluation. Same fallback order as ``judge_model``.

    Returns:
        List of JanitorResult for each evaluated task.
    """
    _guardrails = guardrails_config if guardrails_config is not None else GuardrailsConfig()
    _bypass_guardrails = permission_mode == "bypass"
    # Attribution needs a git repo; detect once per janitor pass. Non-git
    # workdirs (unit tests, dry runs) skip the empty-diff guard entirely --
    # signals-only judgment there is unchanged behavior.
    _attribution_possible = _is_git_repo(workdir)
    results: list[JanitorResult] = []
    for task in tasks:
        if not task.completion_signals:
            continue

        judge_verdict: JudgeVerdict | None = None

        if task.task_type == TaskType.UPGRADE_PROPOSAL:
            all_passed, failed_descs = verify_upgrade_task(task, workdir)
            signal_results: list[tuple[str, bool, str]] = (
                [("upgrade:verified", True, "")] if all_passed else [(f"upgrade:{d}", False, "") for d in failed_descs]
            )
        else:
            signal_results = _collect_signal_results(task, workdir)
            judge_verdict = await _evaluate_judge_signals(
                task,
                workdir,
                signal_results,
                judge_model=judge_model,
                judge_provider=judge_provider,
            )
            all_passed = all(passed for _, passed, _ in signal_results)
            failed_descs = [desc for desc, passed, _ in signal_results if not passed]

        # Empty-diff guard + attribution (item 15 / S2): a non-no-op task
        # with zero attributable changed files must NOT pass -- catches both
        # the 0-file manager rubber-stamp and crash-recovery orphan
        # auto-completions with no diff.
        attributed_commits: list[str] = []
        attributed_files: list[str] = []
        attribution_reason = "attribution skipped: workdir is not a git repo"
        if _attribution_possible:
            attributed_commits, attributed_files, attribution_reason = _attribute_task_work(task, workdir)
            logger.info(
                "janitor attribution: task=%s commits=%s files=%s reason=%s",
                task.id,
                [c[:12] for c in attributed_commits],
                attributed_files,
                attribution_reason,
            )
            if not attributed_files and task.task_type not in _NOOP_TASK_TYPES:
                # An empty attributable diff normally means a 0-file
                # rubber-stamp or a crash-recovery orphan auto-completion and
                # must be rejected. BUT a task can legitimately land real work
                # in a commit whose message omits the task id and which carries
                # no owned_files (attribution then returns empty even though
                # work happened). To avoid false-rejecting such a real
                # completion, only hard-reject when the task has NOT already
                # proven work via a non-trivial passing signal (a passing
                # test_passes / file_contains / llm_review / llm_judge). When
                # such proof exists and all signals passed, downgrade to a
                # warn-and-flag for review instead of failing the task.
                has_nontrivial_pass = all_passed and _has_nontrivial_passing_signal(task, signal_results)
                if has_nontrivial_pass:
                    warn_detail = f"empty diff but non-trivial completion signals passed: {attribution_reason}"
                    signal_results.append(("attribution:empty_diff_warn", True, warn_detail))
                    logger.warning(
                        "janitor WARN (empty diff, flagged for review): task=%s task_type=%s -- no "
                        "changed files attributable to this task (attribution: %s), but non-trivial "
                        "completion signals passed, so accepting rather than rejecting; stamp the "
                        "task id into the landing commit or set owned_files to make attribution "
                        "explicit",
                        task.id,
                        task.task_type.value,
                        attribution_reason,
                    )
                else:
                    all_passed = False
                    empty_diff_detail = f"empty diff: {attribution_reason}"
                    signal_results.append(("attribution:empty_diff", False, empty_diff_detail))
                    failed_descs.append(f"attribution:empty_diff: {empty_diff_detail}")
                    logger.warning(
                        "janitor REJECT (empty diff): task=%s task_type=%s -- no changed files "
                        "attributable to this task (attribution: %s); a task with zero changed "
                        "files must not be accepted unless it is an explicit no-op task type %s "
                        "or it has a non-trivial passing completion signal",
                        task.id,
                        task.task_type.value,
                        attribution_reason,
                        sorted(t.value for t in _NOOP_TASK_TYPES),
                    )

        diff = _get_git_diff(task, workdir, attributed_commits=attributed_commits)
        guardrail_results: list[GuardrailResult] = run_guardrails(
            diff,
            task,
            _guardrails,
            workdir,
            bypass_enabled=_bypass_guardrails,
        )

        blocked_guards = [r for r in guardrail_results if r.blocked and not r.passed]
        if blocked_guards:
            all_passed = False
            for gr in blocked_guards:
                signal_results.append((f"guardrail:{gr.check}", False, gr.detail))
                failed_descs.append(f"guardrail:{gr.check}: {gr.detail}")
            logger.warning(
                "janitor REJECT (guardrail): task=%s blocked_checks=%s",
                task.id,
                [(gr.check, gr.detail) for gr in blocked_guards],
            )

        fix_task_ids = await _create_fix_tasks_if_needed(
            task,
            all_passed,
            failed_descs,
            judge_verdict,
            server_url,
            workdir,
        )

        # Single-line accept/reject decision with WHY -- inputs (signal
        # results) and the verdict, so the janitor's per-task disposition is
        # visible from the log alone without cross-referencing JanitorResult.
        if all_passed:
            logger.info(
                "janitor ACCEPT: task=%s signals=%s attributed_commits=%s attributed_files=%s attribution=%s",
                task.id,
                [(desc, ok) for desc, ok, _ in signal_results],
                [c[:12] for c in attributed_commits],
                attributed_files,
                attribution_reason,
            )
        else:
            logger.warning(
                "janitor REJECT: task=%s failed_signals=%s fix_tasks_created=%s judge_verdict=%s "
                "attributed_commits=%s attributed_files=%s attribution=%s",
                task.id,
                failed_descs,
                fix_task_ids,
                judge_verdict.verdict if judge_verdict else None,
                [c[:12] for c in attributed_commits],
                attributed_files,
                attribution_reason,
            )

        results.append(
            JanitorResult(
                task_id=task.id,
                passed=all_passed,
                signal_results=signal_results,
                fix_tasks_created=fix_task_ids,
                judge_verdict=judge_verdict,
                guardrail_results=guardrail_results,
            )
        )
    return results


def verify_upgrade_task(task: Task, workdir: Path) -> tuple[bool, list[str]]:
    """Verify an upgrade task was executed correctly.

    Checks:
    1. Upgrade transaction exists and completed
    2. Git commit was made (if applicable)
    3. Rollback is available if needed

    Args:
        task: Upgrade proposal task.
        workdir: Project working directory.

    Returns:
        Tuple of (all_passed, list_of_failed_checks).
    """
    failed: list[str] = []

    # Check if upgrade details exist
    if not task.upgrade_details:
        failed.append("No upgrade details found")
        return False, failed

    # Verify rollback plan exists for high-risk upgrades
    risk = task.upgrade_details.risk_assessment.level
    if risk in ("high", "critical") and not task.upgrade_details.rollback_plan.steps:
        failed.append(f"Missing rollback plan for {risk}-risk upgrade")

    # Check completion signals as normal
    for signal in task.completion_signals:
        passed, _detail = evaluate_signal(signal, workdir)
        if not passed:
            failed.append(f"{signal.type}: {signal.value}")

    return len(failed) == 0, failed


async def create_fix_tasks(
    task: Task,
    failed_signals: list[str],
    server_url: str,
    *,
    workdir: Path | None = None,
) -> list[str]:
    """Create fix tasks for failed signals and POST them to the task server.

    Args:
        task: The original task that failed verification.
        failed_signals: Human-readable descriptions of which signals failed.
        server_url: Base URL of the task server (e.g. "http://localhost:8052").
        workdir: Optional repo root for completion-budget enforcement.

    Returns:
        List of task IDs created on the server.
    """
    created_ids: list[str] = []
    url = f"{server_url.rstrip('/')}/tasks"
    budget: CompletionBudget | None = None
    if workdir is not None:
        budget = CompletionBudget(workdir)
        should_create, reason = budget.should_create_fix_task(task)
        if not should_create:
            logger.warning("Task %s: not creating janitor fix task - %s", task.id, reason)
            return []

    bullet_list = "\n".join(f"  - {s}" for s in failed_signals)
    body = {
        "title": f"Fix: {task.title} (janitor)",
        "description": (
            f"Auto-created by janitor. Original task {task.id} failed verification.\n"
            f"Failed signals:\n{bullet_list}\n\n"
            f"Original description: {task.description}"
        ),
        "role": task.role,
        "priority": task.priority,
        "scope": task.scope.value,
        "complexity": task.complexity.value,
        "estimated_minutes": task.estimated_minutes,
        "depends_on": [],
        "owned_files": task.owned_files,
    }

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(url, json=body)
            resp.raise_for_status()
            data = resp.json()
            created_id: str = data.get("id", uuid.uuid4().hex[:12])
            created_ids.append(created_id)
            if budget is not None:
                budget.record_attempt(task, is_fix=True)
            logger.info("Created fix task %s for failed task %s", created_id, task.id)
    except (httpx.HTTPError, KeyError) as exc:
        logger.warning("Failed to create fix task for %s: %s", task.id, exc)

    return created_ids


# --- Judge ---


def _get_judge_retry_count(task: Task) -> int:
    """Extract judge retry count from task description."""
    match = _JUDGE_RETRY_RE.search(task.description)
    return int(match.group(1)) if match else 0


def _get_git_diff(task: Task, workdir: Path, *, attributed_commits: list[str] | None = None) -> str:
    """Get git diff for the task's work, truncated for cost control.

    When *attributed_commits* is provided (commits whose message references
    the task id), the diff is the content of exactly those commits -- the
    work belonging to THE TASK BEING JUDGED. Otherwise falls back to the
    historical behavior (last-commit diff over owned_files), which can show
    another task's landing commit and is only used when no attribution
    exists.
    """
    try:
        if attributed_commits:
            # The commits themselves are already attributed to this task --
            # no owned_files path filter, their full content IS the work.
            cmd = ["git", "show", "--format=", *attributed_commits]
        else:
            cmd = ["git", "diff", "HEAD~1", "--"]
            if task.owned_files:
                cmd.extend(task.owned_files)
        result = subprocess.run(
            cmd,
            cwd=workdir,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
        )
        diff = result.stdout.strip()
        if len(diff) > JUDGE_MAX_DIFF_CHARS:
            diff = diff[:JUDGE_MAX_DIFF_CHARS] + "\n... (truncated for cost cap)"
        return diff or "(no diff available)"
    except (subprocess.TimeoutExpired, OSError) as exc:
        logger.warning("Failed to get git diff: %s", exc)
        return "(failed to get git diff)"


def _render_judge_prompt(task: Task, diff: str, criteria: str) -> str:
    """Render the judge prompt template with task context."""
    from bernstein.templates.renderer import render_template

    context = {
        "TASK_TITLE": task.title,
        "TASK_DESCRIPTION": task.description,
        "CRITERIA": criteria,
        "GIT_DIFF": diff,
    }
    return render_template(_JUDGE_TEMPLATE_PATH, context)


def _parse_judge_response(raw: str) -> JudgeVerdict:
    """Parse the LLM judge response JSON into a JudgeVerdict.

    Handles common response quirks (markdown fences, extra text).
    Returns a retry verdict with low confidence on parse failure.
    """
    text = raw.strip()

    # Strip markdown code fences if present
    if text.startswith("```"):
        lines = text.splitlines()
        # Remove first and last fence lines
        lines = [line for line in lines if not line.strip().startswith("```")]
        text = "\n".join(lines).strip()

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        # Try to extract JSON from surrounding text
        start = text.find("{")
        end = text.rfind("}") + 1
        if start >= 0 and end > start:
            try:
                data = json.loads(text[start:end])
            except json.JSONDecodeError:
                logger.warning("Failed to parse judge response: %s", text[:200])
                return JudgeVerdict(
                    verdict="retry",
                    confidence=0.0,
                    feedback=f"Judge response was not valid JSON: {text[:200]}",
                    flagged_for_review=True,
                )
        else:
            logger.warning("No JSON found in judge response: %s", text[:200])
            return JudgeVerdict(
                verdict="retry",
                confidence=0.0,
                feedback=f"Judge response contained no JSON: {text[:200]}",
                flagged_for_review=True,
            )

    verdict_raw = str(data.get("verdict", "retry")).lower()
    verdict_str: Literal["accept", "retry"] = "retry"
    if verdict_raw == "accept":
        verdict_str = "accept"

    confidence = float(data.get("confidence", 0.5))
    confidence = max(0.0, min(1.0, confidence))

    feedback = str(data.get("feedback", ""))
    flagged = confidence < JUDGE_CONFIDENCE_THRESHOLD

    return JudgeVerdict(
        verdict=verdict_str,
        confidence=confidence,
        feedback=feedback,
        flagged_for_review=flagged,
    )


async def judge_task(
    task: Task,
    workdir: Path,
    criteria: str,
    *,
    judge_model: str | None = None,
    judge_provider: str | None = None,
) -> JudgeVerdict:
    """Evaluate task completion using an LLM judge.

    Gets the git diff, renders the judge prompt, calls the LLM, and parses
    the structured verdict. Cost-capped at ~$0.10 per invocation via
    diff truncation and response token limits.

    Args:
        task: The completed task to judge.
        workdir: Project root directory.
        criteria: Evaluation criteria string from the llm_judge signal.
        judge_model: Optional model override. Falls back to ``JUDGE_MODEL``
            when unset (the operator's ``bernstein.yaml`` judge_model, if
            any, should already be passed in here by the caller).
        judge_provider: Optional provider override. Falls back to
            ``JUDGE_PROVIDER`` when unset.

    Returns:
        JudgeVerdict with accept/retry decision, confidence, and feedback.
    """
    diff = _get_git_diff(task, workdir)
    prompt = _render_judge_prompt(task, diff, criteria)

    model = judge_model or JUDGE_MODEL
    provider = judge_provider or JUDGE_PROVIDER
    logger.info(
        "judge_task: resolved model=%s provider=%s (override_model=%s override_provider=%s) task=%s",
        model,
        provider,
        judge_model,
        judge_provider,
        task.id,
    )

    try:
        raw_response = await call_llm(
            prompt=prompt,
            model=model,
            provider=provider,
            max_tokens=JUDGE_MAX_TOKENS,
            temperature=0.0,
        )
    except RuntimeError as exc:
        logger.error("Judge LLM call failed for task %s: %s", task.id, exc)
        return JudgeVerdict(
            verdict="retry",
            confidence=0.0,
            feedback=f"Judge LLM call failed: {exc}",
            flagged_for_review=True,
        )

    if not raw_response.strip():
        return JudgeVerdict(
            verdict="retry",
            confidence=0.0,
            feedback="Judge returned empty response",
            flagged_for_review=True,
        )

    verdict = _parse_judge_response(raw_response)
    logger.info(
        "Judge verdict for task %s: %s (confidence=%.2f, flagged=%s)",
        task.id,
        verdict.verdict,
        verdict.confidence,
        verdict.flagged_for_review,
    )
    return verdict


async def _create_judge_fix_task(
    task: Task,
    verdict: JudgeVerdict,
    retry_count: int,
    server_url: str,
    *,
    workdir: Path | None = None,
) -> list[str]:
    """Create a fix task from a judge RETRY verdict with feedback.

    Embeds the retry count marker [judge_retry:N] in the description
    so subsequent judge runs can enforce the max retry limit.

    Args:
        task: The original task that the judge wants retried.
        verdict: The JudgeVerdict with feedback.
        retry_count: Current retry count (0-based).
        server_url: Base URL of the task server.
        workdir: Optional repo root for completion-budget enforcement.

    Returns:
        List of created task IDs (0 or 1).
    """
    next_retry = retry_count + 1
    created_ids: list[str] = []
    url = f"{server_url.rstrip('/')}/tasks"
    budget: CompletionBudget | None = None
    if workdir is not None:
        budget = CompletionBudget(workdir)
        should_create, reason = budget.should_create_fix_task(task)
        if not should_create:
            logger.warning("Task %s: not creating judge fix task - %s", task.id, reason)
            return []

    body = {
        "title": f"Fix: {task.title} (judge retry {next_retry})",
        "description": (
            f"[judge_retry:{next_retry}] Auto-created by LLM judge.\n"
            f"Original task {task.id} received RETRY verdict "
            f"(confidence: {verdict.confidence:.2f}).\n\n"
            f"**Judge feedback:**\n{verdict.feedback}\n\n"
            f"Original description: {task.description}"
        ),
        "role": task.role,
        "priority": task.priority,
        "scope": task.scope.value,
        "complexity": task.complexity.value,
        "estimated_minutes": task.estimated_minutes,
        "depends_on": [],
        "owned_files": task.owned_files,
    }

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(url, json=body)
            resp.raise_for_status()
            data = resp.json()
            created_id: str = data.get("id", uuid.uuid4().hex[:12])
            created_ids.append(created_id)
            if budget is not None:
                budget.record_attempt(task, is_fix=True)
            logger.info(
                "Created judge fix task %s (retry %d) for task %s",
                created_id,
                next_retry,
                task.id,
            )
    except (httpx.HTTPError, KeyError) as exc:
        logger.warning("Failed to create judge fix task for %s: %s", task.id, exc)

    return created_ids


# --- Signal implementations ---


def _resolve(path_str: str, workdir: Path) -> Path:
    """Resolve a path string relative to workdir."""
    p = Path(path_str)
    if p.is_absolute():
        return p
    return workdir / p


# --- issue #2760: worktree-internal signal paths ---
#
# Agent worktrees live under ``<workdir>/.sdd/worktrees/<session>/`` (legacy)
# or ``<workdir>/.sdd/runtime/worktrees/<session>/`` and are deleted after the
# run. A filesystem signal whose path points inside one of them (managers
# routinely emit the path the agent reported, which is worktree-absolute) only
# proves the agent wrote the file at some instant -- not that the git-based
# merge-back delivered it to the operator checkout. If the file is
# gitignore-excluded, merge-back stages nothing, the worktree is deleted, and
# a naive check would still report done/HEALTHY with zero deliverable.
#
# Fix: translate any worktree-internal signal path to its repo-relative form
# and verify THAT against the checkout the janitor was pointed at. Existence
# only inside a worktree never counts as delivered; glob and fuzzy fallbacks
# likewise never match worktree-internal files.

_WORKTREE_BASE_MARKERS: tuple[tuple[str, ...], ...] = (
    (".sdd", "runtime", "worktrees"),
    (".sdd", "worktrees"),
)


def _split_worktree_path(path_str: str) -> str | None:
    """Return the repo-relative remainder of a worktree-internal path.

    Recognizes both worktree layouts (``.sdd/worktrees/<session>/...`` and
    ``.sdd/runtime/worktrees/<session>/...``) anywhere in the path, so
    absolute and repo-relative signal values both translate.

    Args:
        path_str: Raw signal path.

    Returns:
        The path relative to the worktree root (i.e. relative to the repo),
        or None when the path does not point inside a worktree or has no
        remainder after the session directory.
    """
    parts = Path(path_str).parts
    for i, part in enumerate(parts):
        if part != ".sdd":
            continue
        for marker in _WORKTREE_BASE_MARKERS:
            if tuple(parts[i : i + len(marker)]) == marker:
                remainder = parts[i + len(marker) + 1 :]
                if remainder:
                    return str(Path(*remainder))
    return None


def _translate_worktree_signal_path(path_str: str, workdir: Path) -> tuple[str, bool]:
    """Translate a worktree-internal signal path to its repo-relative form.

    Args:
        path_str: Raw signal path.
        workdir: Checkout the signal is being verified against.

    Returns:
        Tuple of (path to verify, translated). When ``translated`` is True
        the returned path is repo-relative and must be resolved against
        *workdir*; otherwise ``path_str`` is returned unchanged.
    """
    remainder = _split_worktree_path(path_str)
    if remainder is None:
        return path_str, False
    logger.warning(
        "janitor: signal path %r points inside an ephemeral agent worktree; "
        "verifying repo-relative path %r against %s instead -- existence only "
        "in a worktree does not count as delivered",
        path_str,
        remainder,
        workdir,
    )
    return remainder, True


def _is_worktree_internal(path: Path, workdir: Path) -> bool:
    """True when *path* is inside one of *workdir*'s agent worktree bases."""
    return any(path.is_relative_to(workdir.joinpath(*marker)) for marker in _WORKTREE_BASE_MARKERS)


def _gitignore_exclusion(path_str: str, workdir: Path) -> str | None:
    """Return the gitignore rule excluding *path_str* from git, if any.

    Args:
        path_str: Path to test, relative to *workdir* or absolute inside it.
        workdir: Repository root to run ``git check-ignore`` in.

    Returns:
        The matching rule as ``<source>:<line>:<pattern>``, or None when the
        path is not ignored, lies outside *workdir*, or git is unavailable.
    """
    candidate = Path(path_str)
    if candidate.is_absolute():
        try:
            candidate = candidate.relative_to(workdir)
        except ValueError:
            return None
    try:
        result = subprocess.run(
            ["git", "check-ignore", "--verbose", "--", str(candidate)],
            cwd=workdir,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        logger.debug("janitor: git check-ignore failed for %r in %s: %s", path_str, workdir, exc)
        return None
    if result.returncode != 0:
        return None
    lines = result.stdout.strip().splitlines()
    if not lines:
        return None
    # Output format: <source>:<linenum>:<pattern>\t<pathname>
    return lines[0].split("\t", 1)[0]


def _path_miss_detail(path_str: str, workdir: Path, worktree_original: str | None) -> str:
    """Build the failure detail for a path_exists miss.

    Keeps the bare ``"not found"`` wire format for the ordinary case; adds
    an actionable cause when the signal path pointed inside an ephemeral
    worktree and/or the deliverable is excluded by a gitignore rule (so the
    git-based merge-back never staged it).

    Args:
        path_str: The path that was actually verified (post-translation).
        workdir: Checkout the path was verified against.
        worktree_original: The raw signal value when it pointed inside a
            worktree, else None.

    Returns:
        Human-readable failure detail.
    """
    rule = _gitignore_exclusion(path_str, workdir)
    if rule is None and worktree_original is None:
        return "not found"
    fragments: list[str] = []
    if worktree_original is not None:
        fragments.append(
            f"signal path {worktree_original!r} pointed inside an ephemeral agent worktree; "
            f"repo-relative path {path_str!r} does not exist in the operator checkout"
        )
    else:
        fragments.append(f"{path_str!r} does not exist in the operator checkout")
    if rule is not None:
        fragments.append(
            f"the path matches gitignore rule [{rule}], so the git-based merge-back never "
            "stages it; write the deliverable to a non-ignored path or amend the ignore rule"
        )
    return "not found: " + "; ".join(fragments)


_GLOB_CHARS = frozenset("*?[")


def _fuzzy_paths_enabled() -> bool:
    """Whether the OPT-IN fuzzy basename fallback for ``path_exists`` is active.

    Issue #2186: manager decompositions guess exact file paths at planning
    time (e.g. ``packages/db/test/seed-workers.test.ts``), before any code
    exists. Workers legitimately place files at repo-idiomatic paths (e.g.
    ``packages/db/src/__tests__/seed-workers-demo-link.test.ts``). A literal
    miss then fails the janitor, drains the retry/error budget, and can
    trip a sev1 orchestration pause even though the work was done correctly.

    Default OFF: exact-path matching is the contract operators rely on --
    "the check means exactly the path I wrote." Set
    ``BERNSTEIN_JANITOR_FUZZY_PATHS=1`` to opt in to the fuzzy basename
    fallback for runs where the manager's decomposition guesses paths.
    Explicit glob syntax in the criterion itself (``*``, ``?``, ``[``) is
    honored regardless of this flag -- that's opt-in per-check by
    construction.
    """
    raw = os.environ.get("BERNSTEIN_JANITOR_FUZZY_PATHS", "0").strip().lower()
    enabled = raw in {"1", "true", "yes", "on"}
    if raw not in {"0", "1", "false", "true", "no", "yes", "off", "on"}:
        logger.warning(
            "BERNSTEIN_JANITOR_FUZZY_PATHS=%r not recognized; fuzzy path matching stays disabled",
            raw,
        )
    return enabled


def _check_path_exists(path_str: str, workdir: Path) -> tuple[bool, str]:
    """Check if a file or directory exists.

    Matching is conservative (issue #2186):
      1. Literal path, resolved against ``workdir`` -- the default and
         only out-of-the-box behavior, unchanged: the check means exactly
         the path the plan author wrote.
      2. If the literal path does NOT exist AND the criterion contains
         explicit glob syntax (``*``/``?``/``[``), it is evaluated as a glob
         pattern. Literal semantics take precedence: a literal path that
         happens to contain a metacharacter (e.g. a Next.js dynamic-route
         file ``app/users/[id]/page.tsx`` or a fixture ``foo[1].json``) is
         matched literally first and is never silently reinterpreted as a
         glob when it exists on disk.
      3. OPT-IN fuzzy fallback (``BERNSTEIN_JANITOR_FUZZY_PATHS=1``,
         default off): on a literal miss, try a pattern derived from the
         basename (``**/<stem>*<suffix>``) to tolerate a manager guessing
         the wrong directory convention (e.g. ``test/`` vs
         ``src/__tests__/``) for a file the worker did create. When it
         fires, a WARNING log names the literal miss, the pattern, and the
         path that satisfied the check.

    Delivery contract (issue #2760): a signal path pointing inside an
    ephemeral agent worktree is first translated to its repo-relative form
    (:func:`_translate_worktree_signal_path`) and verified against
    ``workdir``; glob and fuzzy fallbacks never match worktree-internal
    files. Existence only in a worktree is not delivery -- the worktree is
    deleted after the run.

    Returns:
        (passed, detail). ``detail`` is "exists"/"not found" for the
        literal case (unchanged wire format) and a longer message
        identifying the matched path when a glob or fuzzy match passed,
        or naming the delivery failure cause (worktree-only path,
        gitignore exclusion) when that is determinable on a miss.
    """
    original = path_str
    path_str, translated = _translate_worktree_signal_path(path_str, workdir)
    worktree_original = original if translated else None

    has_glob_syntax = any(c in _GLOB_CHARS for c in path_str)

    # Literal-first: a literal path always wins, even when it contains a glob
    # metacharacter. This preserves literal-path semantics for real files like
    # ``app/users/[id]/page.tsx`` or ``foo[1].json`` so they are never silently
    # reinterpreted as a glob when they exist on disk.
    literal = _resolve(path_str, workdir)
    if literal.exists():
        return True, "exists"

    if has_glob_syntax:
        # Literal miss AND the criterion contains explicit glob syntax: honor
        # it as a glob pattern (opt-in per-check by construction). Matches
        # inside an agent worktree are excluded -- they are not deliveries.
        glob_pattern = str(workdir / path_str)
        glob_matches = sorted(
            m for m in globmod.glob(glob_pattern, recursive=True) if not _is_worktree_internal(Path(m), workdir)
        )
        if glob_matches:
            matched = glob_matches[0]
            logger.info(
                "janitor path_exists: glob pattern %r matched %r",
                path_str,
                matched,
            )
            return True, f"exists: glob pattern matched {matched}"
        logger.info(
            "janitor path_exists: glob pattern %r matched nothing under %s",
            path_str,
            workdir,
        )
        return False, _path_miss_detail(path_str, workdir, worktree_original)

    if not _fuzzy_paths_enabled():
        return False, _path_miss_detail(path_str, workdir, worktree_original)

    # Opt-in fuzzy basename fallback. Split the basename on the FIRST dot,
    # not the last: multi-part suffixes like ".test.ts" / ".spec.tsx" are
    # common in this exact scenario (a manager guessing
    # "seed-workers.test.ts" vs. a worker naming it
    # "seed-workers-demo-link.test.ts") and Path.stem/.suffix would only
    # peel off ".ts", leaving ".test" glued onto a stem that then fails to
    # prefix-match the worker's actual filename.
    name = Path(path_str).name
    name_parts = name.split(".", 1)
    stem = name_parts[0]
    suffix = f".{name_parts[1]}" if len(name_parts) > 1 else ""
    if not stem:
        logger.info(
            "janitor path_exists: literal miss for %r and no basename to fuzzy-match",
            path_str,
        )
        return False, _path_miss_detail(path_str, workdir, worktree_original)
    fuzzy_pattern = f"**/{stem}*{suffix}" if suffix else f"**/{stem}*"
    full_fuzzy_pattern = str(workdir / fuzzy_pattern)
    fuzzy_matches = sorted(
        m for m in globmod.glob(full_fuzzy_pattern, recursive=True) if not _is_worktree_internal(Path(m), workdir)
    )
    if fuzzy_matches:
        matched = fuzzy_matches[0]
        logger.warning(
            "janitor path_exists: FUZZY MATCH (BERNSTEIN_JANITOR_FUZZY_PATHS=1): "
            "literal path %r not found; fuzzy pattern %r matched %r -- "
            "the check passed on a fuzzy match, not the exact path written in the plan",
            path_str,
            fuzzy_pattern,
            matched,
        )
        return True, f"exists via fuzzy fallback: matched {matched} (basename pattern: {fuzzy_pattern})"

    logger.info(
        "janitor path_exists: no match for %r (tried literal and fuzzy %r; fuzzy matching enabled)",
        path_str,
        fuzzy_pattern,
    )
    return False, _path_miss_detail(path_str, workdir, worktree_original)


def _check_glob_exists(pattern: str, workdir: Path) -> bool:
    """Check if at least one file matches the glob pattern.

    Worktree-internal signal patterns are translated to their repo-relative
    form and matches inside an agent worktree are excluded (issue #2760):
    the worktree is deleted after the run, so files there are not deliveries.
    """
    pattern, _ = _translate_worktree_signal_path(pattern, workdir)
    full_pattern = str(workdir / pattern)
    matches = [m for m in globmod.glob(full_pattern, recursive=True) if not _is_worktree_internal(Path(m), workdir)]
    return len(matches) > 0


# --- bug 12: janitor branch-check acceptance signals vs actual pushed branch ---
#
# Task acceptance signals of type ``test_passes`` are sometimes generated as
# git branch/commit checks, e.g. ``git rev-parse --verify fix-905-x`` or
# ``git log -1 --format=%s fix-905-x | grep -q '#905'``. The branch name
# baked into the signal comes from a slugifier at task-generation time
# (hyphen-separated), while the agent that actually does the work is free to
# choose its own branch name and conventionally uses a slash after the type
# prefix (``fix/905-x``, ``feat/905-x``). When the two diverge, the literal
# shell command fails even though the agent's work (including the pushed
# branch and PR) is real -- bernstein records its own success as a failure
# and DLQs the task. See work/agent-reports/2026-07-02-run9-attempt9-audit.md
# (task d56c18f2fc08): expected ``fix-905-demo-worker-record`` (hyphens),
# agent pushed ``fix/905-demo-worker-record`` (slash); PR #965 was open and
# real the entire time.
#
# Fix: for git branch/commit-check commands, resolve the referenced token
# against branches that actually exist (local + remote-tracking), tolerant
# of slash-vs-hyphen drift, and rewrite the command to the ref that was
# actually found before running it. Every resolution attempt is logged at
# INFO with the expected ref(s), the branches actually found, and the
# verdict + reason, so a future mismatch is a log read, not a DLQ mystery.

_GIT_REF_CHECK_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^\s*git\s+rev-parse\s+--verify\s"),
    re.compile(r"^\s*git\s+log\s+-1\b"),
)

# Well-known refs that are never task-specific branch names -- never rewrite
# these even if they happen to appear as the trailing bare token.
_SPECIAL_REFS = {"HEAD", "head", "main", "master", "trunk", "develop", "@"}


def _extract_branch_ref(command: str) -> str | None:
    """Best-effort extraction of a branch/ref token from a git test_passes command.

    Only fires for the two acceptance-signal shapes bernstein's task
    generator emits to verify a task pushed a branch: ``git rev-parse
    --verify <ref>`` and ``git log -1 ... <ref> | ...``. The ref is taken as
    the last whitespace-separated, non-flag token in the segment before any
    pipe (flags like ``--format=%s`` are excluded because they start with
    ``-``).

    Args:
        command: The raw ``test_passes`` shell command.

    Returns:
        The extracted ref, or None if the command doesn't look like a
        branch/commit check, or the trailing token is a well-known special
        ref (HEAD/main/master/...) rather than a task-specific branch name.
    """
    if not any(pattern.match(command) for pattern in _GIT_REF_CHECK_PATTERNS):
        return None
    first_segment = command.split("|", 1)[0]
    tokens = first_segment.split()
    candidates = [tok for tok in tokens[2:] if tok and not tok.startswith("-")]
    if not candidates:
        return None
    ref = candidates[-1]
    if ref in _SPECIAL_REFS:
        return None
    return ref


def _list_git_branches(workdir: Path) -> list[str]:
    """Return every local and remote-tracking branch name visible in *workdir*."""
    try:
        result = subprocess.run(
            ["git", "for-each-ref", "--format=%(refname:short)", "refs/heads/", "refs/remotes/"],
            cwd=workdir,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        logger.warning("janitor: git for-each-ref failed in %s: %s", workdir, exc)
        return []
    if result.returncode != 0:
        logger.warning(
            "janitor: git for-each-ref exited %s in %s: %s",
            result.returncode,
            workdir,
            result.stderr.strip(),
        )
        return []
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def _normalize_branch_token(name: str) -> str:
    """Collapse ``/`` to ``-`` so slash- and hyphen-style branch names compare equal."""
    return name.replace("/", "-")


def _resolve_branch_ref(expected: str, workdir: Path) -> tuple[str | None, list[str]]:
    """Resolve *expected* against the branches that actually exist in *workdir*.

    Tries, in order: exact local/remote-tracking match, ``origin/<expected>``,
    then a slash-vs-hyphen-normalized fuzzy match against every branch found
    (this is what recovers bug 12: signal expects ``fix-905-x``, agent pushed
    ``fix/905-x``).

    Args:
        expected: The branch/ref token pulled from the acceptance command.
        workdir: Project root to inspect for branches.

    Returns:
        Tuple of (resolved ref name or None if nothing matched, the full
        list of branches found -- for logging).
    """
    branches = _list_git_branches(workdir)
    if expected in branches:
        return expected, branches
    remote_exact = f"origin/{expected}"
    if remote_exact in branches:
        return remote_exact, branches
    target_norm = _normalize_branch_token(expected)
    for branch in branches:
        bare = branch.split("/", 1)[1] if branch.startswith("origin/") else branch
        if _normalize_branch_token(bare) == target_norm:
            return branch, branches
    return None, branches


def _resolve_branch_check_command(command: str, workdir: Path) -> str:
    """Rewrite a branch-check ``test_passes`` command to the ref that actually exists.

    No-op for commands that aren't recognized branch/commit checks. For
    recognized checks, logs the expected ref, every branch found in
    *workdir*, and the verdict (exact match / rewritten / not found) before
    returning the (possibly rewritten) command for execution.

    Args:
        command: The raw ``test_passes`` shell command from the completion signal.
        workdir: Project root the command will run in.

    Returns:
        The original command, or the same command with the expected ref
        token replaced by the actually-existing branch name.
    """
    expected = _extract_branch_ref(command)
    if expected is None:
        return command

    resolved, branches = _resolve_branch_ref(expected, workdir)

    if resolved == expected:
        logger.info(
            "janitor acceptance check: branch ref %r matched exactly in %s "
            "(branches found: %s) - verdict: PASS-eligible, running command as-is",
            expected,
            workdir,
            branches,
        )
        return command

    if resolved is not None:
        logger.info(
            "janitor acceptance check: branch ref %r not found verbatim, but "
            "%r matched under slash/hyphen normalization in %s (branches "
            "found: %s) - verdict: rewriting command to use the "
            "actually-pushed ref (bug 12: hyphen-vs-slash naming drift "
            "between task generation and agent-side branch naming)",
            expected,
            resolved,
            workdir,
            branches,
        )
        return command.replace(expected, resolved, 1)

    logger.info(
        "janitor acceptance check: branch ref %r not found (exact, "
        "origin/-prefixed, or slash/hyphen variant) in %s (branches found: "
        "%s) - verdict: will FAIL, running original command for an honest error",
        expected,
        workdir,
        branches,
    )
    return command


def _check_test_passes(command: str, workdir: Path) -> bool:
    """Run a shell command and check for exit code 0.

    Args:
        command: Shell command to execute (e.g. "pytest tests/unit/test_foo.py -x").
        workdir: Working directory for the subprocess.

    Returns:
        True if exit code is 0.
    """
    try:
        # SECURITY: shell=True required because janitor commands are internally
        # constructed test invocations (e.g. "pytest tests/...") that may use shell
        # features; not user input.
        result = subprocess.run(
            command,
            shell=True,  # nosemgrep: python.lang.security.audit.subprocess-shell-true.subprocess-shell-true
            cwd=workdir,
            capture_output=True,
            timeout=120,
        )
        if result.returncode != 0:
            logger.info(
                "janitor test_passes FAIL: command=%r cwd=%s exit=%d stderr=%s stdout=%s",
                command,
                workdir,
                result.returncode,
                result.stderr.decode("utf-8", errors="replace")[-2000:],
                result.stdout.decode("utf-8", errors="replace")[-2000:],
            )
        return result.returncode == 0
    except subprocess.TimeoutExpired as exc:
        logger.warning(
            "janitor test_passes TIMEOUT: command=%r cwd=%s timeout=120s exc=%s",
            command,
            workdir,
            exc,
        )
        return False
    except OSError as exc:
        logger.warning(
            "janitor test_passes ERROR: command=%r cwd=%s exc=%s",
            command,
            workdir,
            exc,
        )
        return False


def _check_file_contains(spec: str, workdir: Path) -> bool:
    """Check if a file contains a specific string.

    Format: "path :: needle" -- splits on first " :: " only.

    Args:
        spec: "filepath :: search_string" format.
        workdir: Project root for path resolution.

    Returns:
        True if the file exists and contains the needle.
    """
    parts = spec.split(" :: ", maxsplit=1)
    if len(parts) != 2:
        return False
    path_str, needle = parts
    # Worktree-internal paths verify the delivered copy, not the ephemeral
    # worktree one (issue #2760, same contract as _check_path_exists).
    path_str, _ = _translate_worktree_signal_path(path_str.strip(), workdir)
    target = _resolve(path_str, workdir)
    if not target.is_file():
        return False
    try:
        content = target.read_text(encoding="utf-8")
        return needle in content
    except OSError:
        return False


def _check_llm_review(spec: str, workdir: Path) -> tuple[bool, str]:
    """Spawn a claude CLI call to review files against a spec.

    Args:
        spec: The review instruction (e.g. "Check that the API has proper error handling").
        workdir: Project root -- claude runs from here.

    Returns:
        Tuple of (passed, detail) where detail is the one-line reason from the LLM.
    """
    prompt = (
        f"Review the following files for: {spec}. "
        "Read the files, check the criteria. "
        "Reply ONLY with PASS or FAIL followed by a one-line reason."
    )
    try:
        result = subprocess.run(
            ["claude", "-p", prompt, "--model", "sonnet"],
            cwd=workdir,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
        )
        stdout = result.stdout.strip()
        if not stdout:
            return False, "llm returned empty output"
        first_line = stdout.splitlines()[0].strip()
        if first_line.upper().startswith("PASS"):
            reason = first_line[4:].strip().lstrip("-:").strip()
            return True, reason or "passed"
        if first_line.upper().startswith("FAIL"):
            reason = first_line[4:].strip().lstrip("-:").strip()
            return False, reason or "failed"
        # Ambiguous output -- treat as failure
        return False, f"ambiguous llm output: {first_line[:120]}"
    except subprocess.TimeoutExpired:
        return False, "llm review timed out (60s)"
    except OSError as exc:
        return False, f"failed to spawn claude: {exc}"
