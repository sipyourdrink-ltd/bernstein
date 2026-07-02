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
JUDGE_MODEL = "anthropic/claude-sonnet-4-20250514"
JUDGE_PROVIDER = "openrouter"
JUDGE_MAX_DIFF_CHARS = 10_000  # Truncate diff to control cost
JUDGE_MAX_TOKENS = 1024  # Response token limit (~$0.015 output at Sonnet rates)
JUDGE_CONFIDENCE_THRESHOLD = 0.7  # Below this, flag for human review
_JUDGE_RETRY_RE = re.compile(r"\[judge_retry:(\d+)\]")
_JUDGE_TEMPLATE_PATH = _BUNDLED_TEMPLATES_DIR / "prompts" / "judge.md"


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
            ok = _check_test_passes(signal.value, workdir)
            return ok, "exit 0" if ok else "non-zero exit"
        case "file_contains":
            ok = _check_file_contains(signal.value, workdir)
            return ok, "found" if ok else "not found"
        case "llm_review":
            return _check_llm_review(signal.value, workdir)
        case "llm_judge":
            # llm_judge requires async evaluation - use judge_task() instead.
            return False, "llm_judge requires async evaluation via judge_task()"
    return False, f"unknown signal type: {signal.type}"


def verify_task(task: Task, workdir: Path) -> tuple[bool, list[str]]:
    """Verify all completion signals for a task.

    Args:
        task: Task with completion_signals to check.
        workdir: Project root for path resolution.

    Returns:
        Tuple of (all_passed, list_of_failed_signal_descriptions).
        If no signals defined, returns (True, []).
    """
    failed: list[str] = []
    for signal in task.completion_signals:
        passed, _detail = evaluate_signal(signal, workdir)
        if not passed:
            failed.append(f"{signal.type}: {signal.value}")
    all_passed = len(failed) == 0
    return all_passed, failed


def _collect_signal_results(
    task: Task,
    workdir: Path,
) -> list[tuple[str, bool, str]]:
    """Evaluate all signals and return structured results.

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
    verdict = await judge_task(task, workdir, criteria)
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

    Returns:
        List of JanitorResult for each evaluated task.
    """
    _guardrails = guardrails_config if guardrails_config is not None else GuardrailsConfig()
    _bypass_guardrails = permission_mode == "bypass"
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
            judge_verdict = await _evaluate_judge_signals(task, workdir, signal_results)
            all_passed = all(passed for _, passed, _ in signal_results)
            failed_descs = [desc for desc, passed, _ in signal_results if not passed]

        diff = _get_git_diff(task, workdir)
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

        fix_task_ids = await _create_fix_tasks_if_needed(
            task,
            all_passed,
            failed_descs,
            judge_verdict,
            server_url,
            workdir,
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


def _get_git_diff(task: Task, workdir: Path) -> str:
    """Get git diff for the task's owned files, truncated for cost control."""
    try:
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


async def judge_task(task: Task, workdir: Path, criteria: str) -> JudgeVerdict:
    """Evaluate task completion using an LLM judge (Claude Sonnet).

    Gets the git diff, renders the judge prompt, calls the LLM, and parses
    the structured verdict. Cost-capped at ~$0.10 per invocation via
    diff truncation and response token limits.

    Args:
        task: The completed task to judge.
        workdir: Project root directory.
        criteria: Evaluation criteria string from the llm_judge signal.

    Returns:
        JudgeVerdict with accept/retry decision, confidence, and feedback.
    """
    diff = _get_git_diff(task, workdir)
    prompt = _render_judge_prompt(task, diff, criteria)

    try:
        raw_response = await call_llm(
            prompt=prompt,
            model=JUDGE_MODEL,
            provider=JUDGE_PROVIDER,
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


def _fuzzy_path_match_enabled() -> bool:
    """Whether the fuzzy basename fallback for ``path_exists`` is active.

    Issue #2186: manager decompositions guess exact file paths at planning
    time (e.g. ``packages/db/test/seed-workers.test.ts``), before any code
    exists. Workers legitimately place files at repo-idiomatic paths (e.g.
    ``packages/db/src/__tests__/seed-workers-demo-link.test.ts``). A literal
    miss then fails the janitor, drains the retry/error budget, and can
    trip a sev1 orchestration pause even though the work was done correctly.

    Default ON: the glob and fuzzy-basename fallbacks below are strictly
    more permissive than a bare literal check, so this is safe to leave on.
    Set ``BERNSTEIN_JANITOR_FUZZY_PATH_MATCH=0`` to pin the old
    literal-path-only behavior (e.g. if a project relies on path_exists
    rejecting near-miss paths).
    """
    raw = os.environ.get("BERNSTEIN_JANITOR_FUZZY_PATH_MATCH", "1").strip().lower()
    enabled = raw not in {"0", "false", "no", "off"}
    if raw not in {"0", "1", "false", "true", "no", "yes", "off", "on"}:
        logger.warning(
            "BERNSTEIN_JANITOR_FUZZY_PATH_MATCH=%r not recognized; defaulting to enabled",
            raw,
        )
    return enabled


def _check_path_exists(path_str: str, workdir: Path) -> tuple[bool, str]:
    """Check if a file or directory exists, tolerating path-guess drift.

    Three attempts, in order (issue #2186):
      1. Literal path, resolved against ``workdir`` -- the original,
         backwards-compatible behavior.
      2. The same string interpreted as a glob pattern -- covers a manager
         guessing a wildcard-shaped path.
      3. A fuzzy fallback pattern derived from the path's basename
         (``**/<stem>*<suffix>``) -- covers a manager guessing the wrong
         directory convention (e.g. ``test/`` vs ``src/__tests__/``) for a
         file the worker did create. Gated by
         ``BERNSTEIN_JANITOR_FUZZY_PATH_MATCH`` (default on).

    Every fallback match is logged with the literal miss, the pattern
    tried, and the path that satisfied it, so a janitor failure (or a
    fallback rescue) is diagnosable from logs alone.

    Returns:
        (passed, detail). ``detail`` is "exists"/"not found" for the
        literal case (unchanged wire format) and a longer message
        identifying the matched path when a fallback is what passed.
    """
    literal = _resolve(path_str, workdir)
    if literal.exists():
        return True, "exists"

    glob_pattern = str(workdir / path_str)
    glob_matches = sorted(globmod.glob(glob_pattern, recursive=True))
    if glob_matches:
        matched = glob_matches[0]
        logger.info(
            "janitor path_exists: literal miss for %r; glob pattern %r matched %r",
            path_str,
            glob_pattern,
            matched,
        )
        return True, f"exists via glob fallback: matched {matched} (pattern: {path_str})"

    if not _fuzzy_path_match_enabled():
        logger.info(
            "janitor path_exists: no match for %r (literal + glob both missed; "
            "fuzzy fallback disabled via BERNSTEIN_JANITOR_FUZZY_PATH_MATCH=0)",
            path_str,
        )
        return False, "not found"

    # Split on the FIRST dot, not the last: multi-part suffixes like
    # ".test.ts" / ".spec.tsx" are common in this exact scenario (a manager
    # guessing "seed-workers.test.ts" vs. a worker naming it
    # "seed-workers-demo-link.test.ts") and Path.stem/.suffix would only
    # peel off ".ts", leaving ".test" glued onto a stem that then fails to
    # prefix-match the worker's actual filename.
    name = Path(path_str).name
    name_parts = name.split(".", 1)
    stem = name_parts[0]
    suffix = f".{name_parts[1]}" if len(name_parts) > 1 else ""
    if not stem:
        logger.info(
            "janitor path_exists: no match for %r (literal + glob missed; no basename to fuzzy-match)",
            path_str,
        )
        return False, "not found"
    fuzzy_pattern = f"**/{stem}*{suffix}" if suffix else f"**/{stem}*"
    full_fuzzy_pattern = str(workdir / fuzzy_pattern)
    fuzzy_matches = sorted(globmod.glob(full_fuzzy_pattern, recursive=True))
    if fuzzy_matches:
        matched = fuzzy_matches[0]
        logger.info(
            "janitor path_exists: literal+glob miss for %r; fuzzy pattern %r matched %r",
            path_str,
            fuzzy_pattern,
            matched,
        )
        return True, f"exists via fuzzy fallback: matched {matched} (basename pattern: {fuzzy_pattern})"

    logger.info(
        "janitor path_exists: no match for %r (tried literal, glob %r, fuzzy %r)",
        path_str,
        glob_pattern,
        fuzzy_pattern,
    )
    return False, "not found"


def _check_glob_exists(pattern: str, workdir: Path) -> bool:
    """Check if at least one file matches the glob pattern."""
    full_pattern = str(workdir / pattern)
    matches = globmod.glob(full_pattern, recursive=True)
    return len(matches) > 0


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
        return result.returncode == 0
    except (subprocess.TimeoutExpired, OSError):
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
    target = _resolve(path_str.strip(), workdir)
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
