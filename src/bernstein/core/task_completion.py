"""Task completion, retry, and post-completion processing.

Extracted from task_lifecycle.py to reduce module size.  Covers everything
that happens *after* an agent finishes work: log parsing, retry escalation,
janitor/quality-gate fan-out, approval gate, merge, and evolution recording.
"""

from __future__ import annotations

import contextlib
import logging
import re
import time
from typing import TYPE_CHECKING, Any

import httpx

from bernstein.core.context import append_decision
from bernstein.core.cross_model_verifier import (
    CrossModelVerifierConfig,
    run_cross_model_verification_sync,
)
from bernstein.core.formal_verification import FormalVerificationConfig, run_formal_verification
from bernstein.core.janitor import verify_task
from bernstein.core.lifecycle import transition_agent
from bernstein.core.metrics import get_collector
from bernstein.core.models import (
    AgentSession,
    Task,
)
from bernstein.core.quality_gates import run_quality_gates
from bernstein.core.rule_enforcer import RulesConfig, load_rules_config, run_rule_enforcement
from bernstein.core.tick_pipeline import (
    CompletionData,
    fail_task,
)

if TYPE_CHECKING:
    import concurrent.futures
    from pathlib import Path

    from bernstein.core.git_ops import MergeResult

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Completion data extraction
# ---------------------------------------------------------------------------


def collect_completion_data(workdir: Path, session: AgentSession) -> CompletionData:
    """Read agent log file and extract structured completion data.

    Parses the agent's runtime log for files_modified and test_results.

    Args:
        workdir: Project working directory.
        session: Agent session whose log to parse.

    Returns:
        Dict with files_modified and test_results keys.
    """
    data: CompletionData = {"files_modified": [], "test_results": {}}
    log_path = workdir / ".sdd" / "runtime" / f"{session.id}.log"
    if not log_path.exists():
        return data

    try:
        log_content = log_path.read_text(encoding="utf-8", errors="replace")
        lines = log_content.splitlines()
        # Extract file modifications (lines like "Modified: path/to/file")
        files_modified: list[str] = []
        for line in lines:
            stripped = line.strip()
            if stripped.startswith("Modified: ") or stripped.startswith("Created: "):
                fpath = stripped.split(": ", 1)[1].strip()
                if fpath and fpath not in files_modified:
                    files_modified.append(fpath)
        data["files_modified"] = files_modified

        # Extract test results (look for pytest-style summary)
        for line in reversed(lines):
            stripped = line.strip()
            if "passed" in stripped or "failed" in stripped:
                data["test_results"] = {"summary": stripped}
                break
    except OSError as exc:
        logger.debug("Could not read agent log %s: %s", log_path, exc)

    return data


# ---------------------------------------------------------------------------
# Task retry / fail
# ---------------------------------------------------------------------------


def maybe_retry_task(
    task: Task,
    *,
    retried_task_ids: set[str],
    max_task_retries: int,
    client: httpx.Client,
    server_url: str,
    quarantine: Any,
) -> bool:
    """Queue a retry for a failed task with model/effort escalation.

    First retry bumps effort one level (low->medium->high->max), keeps model.
    Second retry escalates model (haiku->sonnet->opus) and resets effort to high.

    Args:
        task: The failed task to potentially retry.
        retried_task_ids: Set of task IDs already retried (mutated in-place).
        max_task_retries: Maximum retries allowed.
        client: httpx client.
        server_url: Task server base URL.
        quarantine: QuarantineStore instance.

    Returns:
        True if a retry task was created, False otherwise.
    """
    if task.id in retried_task_ids:
        return False

    # Determine current retry count from title prefix [RETRY N]
    retry_count = 0
    m = re.match(r"^\[RETRY (\d+)\] ", task.title)
    if m:
        retry_count = int(m.group(1))

    if retry_count >= max_task_retries:
        base_title = re.sub(r"^\[RETRY \d+\] ", "", task.title)
        quarantine.record_failure(base_title, "Max retries exhausted")
        logger.warning(
            "Task %r exhausted %d retries -- recorded cross-run failure in quarantine",
            base_title,
            max_task_retries,
        )
        return False

    next_retry = retry_count + 1

    current_model = task.model or "sonnet"
    current_effort = task.effort or "high"

    effort_ladder = ["low", "medium", "high", "max"]
    model_ladder = ["haiku", "sonnet", "opus"]

    from bernstein.core.models import Scope as _Scope

    # High-stakes roles/scopes always get opus/max on any retry
    _high_stakes_roles = ("architect", "security")
    if task.scope == _Scope.LARGE or task.role in _high_stakes_roles:
        new_model = "opus"
        new_effort = "max"
    elif next_retry == 1:
        # First retry: bump effort one level, keep model
        idx = effort_ladder.index(current_effort) if current_effort in effort_ladder else 2
        new_effort = effort_ladder[min(idx + 1, len(effort_ladder) - 1)]
        new_model = current_model
    else:
        # Second+ retry: escalate model, reset effort to high
        model_lower = current_model.lower()
        model_idx = 1  # default to sonnet position
        for i, name in enumerate(model_ladder):
            if name in model_lower:
                model_idx = i
                break
        new_model = model_ladder[min(model_idx + 1, len(model_ladder) - 1)]
        new_effort = "high"

    base_title = re.sub(r"^\[RETRY \d+\] ", "", task.title)
    new_title = f"[RETRY {next_retry}] {base_title}"
    new_description = f"[RETRY {next_retry}] {task.description}"

    # Progressive timeout: each retry multiplies estimated_minutes by (retry_count + 2)
    progressive_minutes = task.estimated_minutes * (retry_count + 2)

    payload: dict[str, Any] = {
        "title": new_title,
        "description": new_description,
        "role": task.role,
        "priority": task.priority,
        "scope": task.scope.value,
        "complexity": task.complexity.value,
        "estimated_minutes": progressive_minutes,
        "model": new_model,
        "effort": new_effort,
    }

    try:
        resp = client.post(f"{server_url}/tasks", json=payload)
        resp.raise_for_status()
        new_task_id = resp.json().get("id", "?")
        retried_task_ids.add(task.id)
        logger.info(
            "Retry %d queued for failed task %s -> %s (model=%s effort=%s)",
            next_retry,
            task.id,
            new_task_id,
            new_model,
            new_effort,
        )
        return True
    except Exception as exc:
        logger.warning("Failed to queue retry for task %s: %s", task.id, exc)
        return False


def retry_or_fail_task(
    task_id: str,
    reason: str,
    *,
    client: httpx.Client,
    server_url: str,
    max_task_retries: int,
    retried_task_ids: set[str],
    tasks_snapshot: dict[str, list[Task]] | None = None,
) -> None:
    """Re-queue a task for retry, or fail it permanently if max retries reached.

    Reads the current retry count from a ``[retry:N]`` marker in the task
    description.  If the count is below ``max_task_retries`` a new open task
    is created (clone of the original with the marker bumped) and the old
    task is failed silently.  Once the limit is hit the task is failed with
    a "Max retries exceeded" reason.

    Args:
        task_id: ID of the task to retry or fail.
        reason: Human-readable reason for the failure / retry.
        client: httpx client.
        server_url: Task server base URL.
        max_task_retries: Maximum number of retries allowed.
        retried_task_ids: Set of already-retried task IDs (mutated in-place).
        tasks_snapshot: Optional pre-fetched tasks snapshot to avoid an
            extra HTTP round-trip when the task is already in cache.
    """
    base = server_url
    max_retries = max_task_retries

    # Try the pre-fetched snapshot first to avoid an extra GET
    task: Task | None = None
    if tasks_snapshot is not None:
        for bucket in tasks_snapshot.values():
            for t in bucket:
                if t.id == task_id:
                    task = t
                    break
            if task is not None:
                break
        if task is not None:
            logger.debug("retry_or_fail_task %s: resolved from tick snapshot", task_id)

    if task is None:
        try:
            resp = client.get(f"{base}/tasks/{task_id}")
            resp.raise_for_status()
            task = Task.from_dict(resp.json())
        except httpx.HTTPError as exc:
            logger.error("retry_or_fail_task: could not fetch task %s: %s", task_id, exc)
            return

    # Dedup: prevent retry fan-out (same task retried multiple times)
    if task_id in retried_task_ids:
        logger.debug("Skipping duplicate retry for task %s", task_id)
        return
    retried_task_ids.add(task_id)

    # Extract current retry count from description marker
    marker_re = re.compile(r"^\[retry:(\d+)\]\s*")
    m = marker_re.match(task.description)
    retry_count = int(m.group(1)) if m else 0
    base_description = marker_re.sub("", task.description)

    if retry_count < max_retries:
        new_description = f"[retry:{retry_count + 1}] {base_description}"
        # Escalate model on retry: large/architect/security always opus/max;
        # other roles: sonnet->opus on 2nd retry, effort->high on 1st retry.
        from bernstein.core.models import Scope as _Scope

        _high_stakes_roles = ("architect", "security")
        if task.scope == _Scope.LARGE or task.role in _high_stakes_roles:
            retry_model = "opus"
            retry_effort = "max"
        elif retry_count >= 1:
            retry_model = "opus"
            retry_effort = "high"
        else:
            retry_model = task.model or "sonnet"
            retry_effort = task.effort or "high"
        # Progressive timeout: each retry multiplies estimated_minutes by (retry_count + 2)
        # so retry 1 doubles the time, retry 2 triples it, giving agents more runway.
        progressive_minutes = task.estimated_minutes * (retry_count + 2)
        task_body: dict[str, Any] = {
            "title": f"[RETRY {retry_count + 1}] {task.title}",
            "description": new_description,
            "role": task.role,
            "priority": task.priority,
            "scope": task.scope.value,
            "complexity": task.complexity.value,
            "estimated_minutes": progressive_minutes,
            "depends_on": task.depends_on,
            "owned_files": task.owned_files,
            "task_type": task.task_type.value,
            "model": retry_model,
            "effort": retry_effort,
        }
        # Preserve completion signals on retry
        if task.completion_signals:
            task_body["completion_signals"] = [{"type": s.type, "value": s.value} for s in task.completion_signals]
        try:
            client.post(f"{base}/tasks", json=task_body).raise_for_status()
            logger.info(
                "Retrying task %s (attempt %d/%d): %s",
                task_id,
                retry_count + 1,
                max_retries,
                reason,
            )
        except httpx.HTTPError as exc:
            logger.error("Failed to re-create task %s for retry: %s", task_id, exc)
            # Fall through to permanent fail
            fail_task(client, base, task_id, f"Max retries exceeded: {reason}")
            return
        # Fail the old task silently (it has been replaced)
        with contextlib.suppress(httpx.HTTPError):
            fail_task(client, base, task_id, f"Retried: {reason}")
    else:
        fail_task(client, base, task_id, f"Max retries exceeded: {reason}")


# ---------------------------------------------------------------------------
# Post-completion processing
# ---------------------------------------------------------------------------


def process_completed_tasks(
    orch: Any,  # Orchestrator instance
    done_tasks: list[Task],
    result: Any,  # TickResult
) -> None:
    """Run janitor verification and record evolution metrics for done tasks.

    Skips tasks already processed in a prior tick. For each new done task,
    submits verify_task() calls in parallel via orch._executor, then
    processes post-verification steps (sync backlog, append decision,
    record evolution) after all verifications complete.

    Args:
        orch: Orchestrator instance.
        done_tasks: Tasks with status "done" fetched from the server.
        result: TickResult accumulator for verified/verification_failures lists.
    """
    # Filter to only new tasks and mark them all processed upfront.
    new_tasks: list[Task] = []
    for task in done_tasks:
        if task.id in orch._processed_done_tasks:
            continue
        orch._processed_done_tasks.add(task.id)
        new_tasks.append(task)

    if not new_tasks:
        return

    # Fan-out: submit all verify_task() calls in parallel.
    verify_futures: dict[str, concurrent.futures.Future[tuple[bool, list[str]]]] = {}
    for task in new_tasks:
        if task.completion_signals:
            verify_futures[task.id] = orch._executor.submit(verify_task, task, orch._workdir)

    # Fan-in: collect results then run sequential post-verification steps.
    for task in new_tasks:
        if task.id in verify_futures:
            passed, failed_signals = verify_futures[task.id].result()
            janitor_passed = passed
            if passed:
                result.verified.append(task.id)
            else:
                result.verification_failures.append((task.id, failed_signals))
        else:
            janitor_passed = True

        session = orch._find_session_for_task(task.id)
        # Track whether this is the first time we're reaping this session so
        # agent-lifetime metrics are recorded exactly once per agent even when
        # an agent owns multiple tasks that all complete in the same tick.
        _agent_just_reaped = session is not None and session.status != "dead"
        if session is not None:
            # Quality gates: lint/type/test checks run after janitor, before approval.
            _qg_config = getattr(orch, "_quality_gate_config", None)
            if janitor_passed and _qg_config is not None:
                _worktree_for_gates = orch._spawner.get_worktree_path(session.id)
                _gate_run_dir = _worktree_for_gates if _worktree_for_gates is not None else orch._workdir
                _qg_result = run_quality_gates(task, _gate_run_dir, orch._workdir, _qg_config)
                if not _qg_result.passed:
                    janitor_passed = False
                    _qg_failed = [
                        f"quality_gate:{r.gate}" for r in _qg_result.gate_results if r.blocked and not r.passed
                    ]
                    with contextlib.suppress(ValueError):
                        result.verified.remove(task.id)
                    result.verification_failures.append((task.id, _qg_failed))
                    logger.info(
                        "Quality gates blocked merge for task %s: %s",
                        task.id,
                        ", ".join(_qg_failed),
                    )

            # Organizational rule enforcement: .bernstein/rules.yaml checks.
            # Runs after quality gates, before cross-model verification.
            if janitor_passed:
                _rules_config: RulesConfig | None = load_rules_config(orch._workdir)
                if _rules_config is not None:
                    _re_worktree = orch._spawner.get_worktree_path(session.id)
                    _re_run_dir = _re_worktree if _re_worktree is not None else orch._workdir
                    _re_result = run_rule_enforcement(task, _re_run_dir, orch._workdir, _rules_config)
                    if not _re_result.passed:
                        janitor_passed = False
                        _re_failed = [f"rule:{v.rule_id}: {v.fix_hint}" for v in _re_result.violations if v.blocked]
                        with contextlib.suppress(ValueError):
                            result.verified.remove(task.id)
                        result.verification_failures.append((task.id, _re_failed))
                        logger.info(
                            "Rule enforcement blocked merge for task %s: %s",
                            task.id,
                            ", ".join(_re_failed),
                        )

            # Cross-model verification: route diff to a different model for review.
            # Runs after quality gates, before the approval gate.
            # Default: enabled (CrossModelVerifierConfig.enabled=True). Set
            # cross_model_verify=CrossModelVerifierConfig(enabled=False) to opt out.
            _cmv_config: CrossModelVerifierConfig = (
                getattr(orch._config, "cross_model_verify", None) or CrossModelVerifierConfig()
            )
            if janitor_passed and _cmv_config.enabled:
                _cmv_worktree = orch._spawner.get_worktree_path(session.id)
                _cmv_path = _cmv_worktree if _cmv_worktree is not None else orch._workdir
                _cmv_writer = session.model_config.model
                _cmv_verdict = run_cross_model_verification_sync(task, _cmv_path, _cmv_writer, _cmv_config)
                if _cmv_verdict.verdict == "request_changes" and _cmv_config.block_on_issues:
                    janitor_passed = False
                    _cmv_issues_str = "; ".join(_cmv_verdict.issues) if _cmv_verdict.issues else _cmv_verdict.feedback
                    with contextlib.suppress(ValueError):
                        result.verified.remove(task.id)
                    result.verification_failures.append((task.id, [f"cross_model_review:{_cmv_issues_str}"]))
                    logger.info(
                        "Cross-model review blocked merge for task %s (reviewer=%s): %s",
                        task.id,
                        _cmv_verdict.reviewer_model,
                        _cmv_verdict.feedback,
                    )
                    # Queue a fix task so the issues get addressed.
                    _cmv_fix_description = (
                        f"Cross-model review flagged issues in task {task.id} "
                        f"({task.title!r}).\n\n"
                        f"**Reviewer:** {_cmv_verdict.reviewer_model}\n"
                        f"**Feedback:** {_cmv_verdict.feedback}\n\n"
                        f"**Issues to fix:**\n"
                        + "\n".join(f"- {i}" for i in _cmv_verdict.issues)
                        + f"\n\nOriginal task description:\n{task.description}\n"
                    )
                    _cmv_fix_body: dict[str, Any] = {
                        "title": f"[REVIEW-FIX] {task.title[:80]}",
                        "description": _cmv_fix_description,
                        "role": task.role,
                        "priority": max(1, task.priority - 1),
                        "scope": "small",
                        "complexity": "medium",
                        "owned_files": task.owned_files,
                    }
                    try:
                        orch._client.post(f"{orch._config.server_url}/tasks", json=_cmv_fix_body).raise_for_status()
                    except httpx.HTTPError as _cmv_exc:
                        logger.warning(
                            "cross_model_verifier: failed to create fix task for %s: %s",
                            task.id,
                            _cmv_exc,
                        )
                else:
                    logger.info(
                        "Cross-model review approved task %s (reviewer=%s)",
                        task.id,
                        _cmv_verdict.reviewer_model,
                    )

            # Formal verification: check Z3/Lean4 properties from bernstein.yaml.
            # Runs after cross-model review, before the approval gate.
            # Skipped when formal_verification section is absent from bernstein.yaml.
            if janitor_passed:
                _fv_config: FormalVerificationConfig | None = getattr(orch, "_formal_verification_config", None)
                if _fv_config is not None and _fv_config.enabled and _fv_config.properties:
                    # Gather files_modified count from completion data for context
                    _fv_completion = collect_completion_data(orch._workdir, session)
                    _fv_files_modified = len(_fv_completion.get("files_modified", []))
                    _fv_test_summary = _fv_completion.get("test_results", {}).get("summary", "")
                    _fv_test_passed = "failed" not in _fv_test_summary.lower() if _fv_test_summary else True
                    _fv_worktree = orch._spawner.get_worktree_path(session.id)
                    _fv_run_dir = _fv_worktree if _fv_worktree is not None else orch._workdir
                    _fv_result = run_formal_verification(
                        task,
                        _fv_run_dir,
                        _fv_config,
                        files_modified=_fv_files_modified,
                        test_passed=_fv_test_passed,
                    )
                    if not _fv_result.passed and not _fv_result.skipped and _fv_config.block_on_violation:
                        janitor_passed = False
                        _fv_failed = [
                            f"formal:{v.property_name}: {v.counterexample[:120]}" for v in _fv_result.violations
                        ]
                        with contextlib.suppress(ValueError):
                            result.verified.remove(task.id)
                        result.verification_failures.append((task.id, _fv_failed))
                        logger.info(
                            "Formal verification blocked merge for task %s: %s",
                            task.id,
                            ", ".join(_fv_failed),
                        )
                    else:
                        logger.info(
                            "Formal verification passed for task %s (%d properties checked)",
                            task.id,
                            _fv_result.properties_checked,
                        )

            orch._record_provider_health(session, success=janitor_passed)
            _skip_merge = False
            if janitor_passed and orch._approval_gate is not None:
                _approval_result = orch._approval_gate.evaluate(
                    task,
                    session_id=session.id,
                )
                if _approval_result.rejected:
                    _skip_merge = True
                    logger.warning(
                        "Approval gate: task %s rejected -- skipping merge for agent %s",
                        task.id,
                        session.id,
                    )
                elif not _approval_result.approved:
                    # PR mode -- create PR then skip local merge
                    _skip_merge = True
                    _worktree_path = orch._spawner.get_worktree_path(session.id)
                    if _worktree_path is not None:
                        # Gather metadata for the PR body
                        _pr_collector = get_collector(orch._workdir / ".sdd" / "metrics")
                        _pr_task_m = _pr_collector.task_metrics.get(task.id)
                        _pr_cost_usd = _pr_task_m.cost_usd if _pr_task_m else 0.0
                        _pr_completion = collect_completion_data(orch._workdir, session)
                        _pr_test_summary = _pr_completion.get("test_results", {}).get("summary", "")
                        _pr_url = orch._approval_gate.create_pr(
                            task,
                            worktree_path=_worktree_path,
                            session_id=session.id,
                            labels=orch._config.pr_labels,
                            role=session.role,
                            model=session.model_config.model,
                            cost_usd=_pr_cost_usd,
                            test_summary=_pr_test_summary,
                        )
                        if _pr_url:
                            logger.info(
                                "Approval gate: PR created for task %s: %s",
                                task.id,
                                _pr_url,
                            )
                    else:
                        logger.warning(
                            "Approval gate PR mode: no worktree for agent %s -- cannot create PR",
                            session.id,
                        )
            _merge_result: MergeResult | None = orch._spawner.reap_completed_agent(session, skip_merge=_skip_merge)
            if session.status != "dead":
                transition_agent(session, "dead", actor="task_completion", reason="task completed, process reaped")
            logger.info("Agent %s finished task %s, process reaped", session.id, task.id)

            # Route merge conflicts to a dedicated resolver agent.
            if (
                _merge_result is not None
                and not _merge_result.success
                and _merge_result.conflicting_files
                and not _skip_merge
            ):
                from bernstein.core.task_claiming import create_conflict_resolution_task

                create_conflict_resolution_task(
                    task,
                    _merge_result.conflicting_files,
                    client=orch._client,
                    server_url=orch._config.server_url,
                    session_id=session.id,
                )
                orch._post_bulletin(
                    "alert",
                    f"merge conflict in {len(_merge_result.conflicting_files)} files — "
                    f"resolver task created (task {task.id})",
                )

        # Record task completion in the operational metrics collector so
        # run summaries and evolution analysis see real duration/success data.
        _collector = get_collector(orch._workdir / ".sdd" / "metrics")
        _task_m = _collector.task_metrics.get(task.id)
        _cost_usd = _task_m.cost_usd if _task_m else 0.0

        # Record cost in the per-run budget tracker and persist to disk.
        _agent_id = session.id if session else "unknown"
        _model = session.model_config.model if session else "unknown"
        _tokens_in = _task_m.tokens_prompt if _task_m else 0
        _tokens_out = _task_m.tokens_completion if _task_m else 0
        orch._cost_tracker.record(
            agent_id=_agent_id,
            task_id=task.id,
            model=_model,
            input_tokens=_tokens_in,
            output_tokens=_tokens_out,
            cost_usd=_cost_usd if _cost_usd > 0 else None,
        )
        try:
            orch._cost_tracker.save(orch._workdir / ".sdd")
        except OSError as exc:
            logger.warning("Failed to persist cost tracker: %s", exc)

        _collector.complete_task(task.id, success=janitor_passed, janitor_passed=janitor_passed, cost_usd=_cost_usd)
        if session is not None:
            # complete_agent_task must be called before end_agent so that
            # end_agent() has non-zero task counts and writes the AGENT_SUCCESS
            # metric to the JSONL file.
            _collector.complete_agent_task(session.id, success=janitor_passed)
            _collector.end_agent(session.id)
            # Record agent lifetime to evolution collector (once per agent).
            if orch._evolution is not None and _agent_just_reaped:
                try:
                    _agent_m = _collector.agent_metrics.get(session.id)
                    _lifetime = round(
                        (time.time() - session.spawn_ts) if session.spawn_ts > 0 else 0.0,
                        2,
                    )
                    _tasks_done = _agent_m.tasks_completed if _agent_m else 0
                    orch._evolution.record_agent_lifetime(
                        agent_id=session.id,
                        role=session.role,
                        lifetime_seconds=_lifetime,
                        tasks_completed=_tasks_done,
                        model=session.model_config.model,
                    )
                except Exception as exc:
                    logger.warning("Evolution record_agent_lifetime failed: %s", exc)

        # Post bulletin: task completed or failed (with janitor result)
        if janitor_passed:
            orch._post_bulletin(
                "status",
                f"task completed: {task.title} ({task.id})",
            )
            orch._notify(
                "task.completed",
                f"Task completed: {task.title}",
                task.result_summary or "",
                task_id=task.id,
                role=task.role,
            )
        else:
            orch._post_bulletin(
                "alert",
                f"task failed janitor: {task.title} ({task.id})",
            )
            orch._notify(
                "task.failed",
                f"Task failed: {task.title}",
                task.result_summary or "Janitor verification did not pass.",
                task_id=task.id,
                role=task.role,
            )

        orch._sync_backlog_file(task)

        if task.result_summary:
            try:
                append_decision(
                    orch._workdir,
                    task.id,
                    task.title,
                    task.result_summary,
                )
            except Exception as exc:
                logger.warning("append_decision failed for task %s: %s", task.id, exc)

        if orch._evolution is not None:
            model = session.model_config.model if session else None
            provider = session.provider if session else None
            duration = (
                (_task_m.end_time - _task_m.start_time)
                if _task_m and _task_m.end_time
                else (time.time() - session.spawn_ts if session and session.spawn_ts > 0 else 0.0)
            )
            try:
                orch._evolution.record_task_completion(
                    task=task,
                    duration_seconds=round(duration, 2),
                    cost_usd=_cost_usd,
                    janitor_passed=janitor_passed,
                    model=model,
                    provider=provider,
                )
            except Exception as exc:
                logger.warning("Evolution record_task_completion failed: %s", exc)

        # Record actual duration for ML duration predictor training.
        # Only record successful completions to avoid training on failure noise.
        if janitor_passed and duration > 0:
            try:
                from bernstein.core.duration_predictor import get_predictor

                _effective_model = session.model_config.model if session else None
                _models_dir = orch._workdir / ".sdd" / "models"
                get_predictor(_models_dir).record_completion(
                    task,
                    actual_duration_seconds=round(duration, 2),
                    model=_effective_model,
                )
            except Exception as exc:
                logger.debug("Duration predictor record_completion failed: %s", exc)
