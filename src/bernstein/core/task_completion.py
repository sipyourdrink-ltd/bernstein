"""Task completion processing.

Extracted from task_lifecycle.py — contains process_completed_tasks,
paired test task enqueueing, git diff helpers, backlog ticket movement,
priority decay, and permission-denied handling.
"""

from __future__ import annotations

import contextlib
import logging
import re
import time
from typing import TYPE_CHECKING, Any, cast

import httpx

from bernstein.core.agent_log_aggregator import AgentLogAggregator
from bernstein.core.completion_budget import CompletionBudget
from bernstein.core.context import append_decision
from bernstein.core.cross_model_verifier import (
    CrossModelVerifierConfig,
    run_cross_model_verification_sync,
)
from bernstein.core.effectiveness import EffectivenessScorer
from bernstein.core.janitor import verify_task
from bernstein.core.lifecycle import transition_agent
from bernstein.core.metrics import get_collector
from bernstein.core.models import (
    AgentSession,
    Task,
    TaskStatus,
)
from bernstein.core.rule_enforcer import RulesConfig, load_rules_config, run_rule_enforcement
from bernstein.core.team_state import TeamStateStore
from bernstein.core.tick_pipeline import (
    CompletionData,
    close_task,
)

if TYPE_CHECKING:
    import concurrent.futures
    from pathlib import Path

    from bernstein.core.git_ops import MergeResult
    from bernstein.core.wal import WALWriter

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Completion data extraction
# ---------------------------------------------------------------------------


def collect_completion_data(workdir: Path, session: AgentSession) -> CompletionData:
    """Read agent log file and extract structured completion data.

    Parses the agent's runtime log into a backward-compatible completion payload.

    Args:
        workdir: Project working directory.
        session: Agent session whose log to parse.

    Returns:
        Dict with files_modified, test_results, and optional log_summary keys.
    """
    aggregator = AgentLogAggregator(workdir)
    summary = aggregator.parse_log(session.id)
    data: CompletionData = {
        "files_modified": list(summary.files_modified),
        "test_results": {},
    }
    if aggregator.log_exists(session.id) and summary.total_lines > 0:
        data["log_summary"] = summary
    if summary.test_summary:
        data["test_results"] = {"summary": summary.test_summary}
    return data


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
    from bernstein.core.task_spawn_bridge import create_conflict_resolution_task

    # Filter to only new tasks and mark them all processed upfront.
    new_tasks: list[Task] = []
    for task in done_tasks:
        if task.id in orch._processed_done_tasks:
            continue
        orch._processed_done_tasks[task.id] = None
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
        _cache_verified = False
        _cache_diff_lines = 0
        _qg_result: Any = None
        if task.id in verify_futures:
            try:
                passed, failed_signals = verify_futures[task.id].result()
            except Exception:
                logger.warning("verify_task raised for %s — treating as failed", task.id)
                passed = False
                failed_signals = ["verify_task exception"]
            janitor_passed = passed
            if passed:
                result.verified.append(task.id)
            else:
                result.verification_failures.append((task.id, failed_signals))
        else:
            # No completion_signals defined — auto-pass and count as verified.
            janitor_passed = True
            result.verified.append(task.id)

        # WAL: record task completion/failure decision
        _wal_c: WALWriter | None = getattr(orch, "_wal_writer", None)
        if _wal_c is not None:
            _wal_dtype = "task_completed" if janitor_passed else "task_failed"
            try:
                _wal_c.write_entry(
                    decision_type=_wal_dtype,
                    inputs={"task_id": task.id, "title": task.title, "role": task.role},
                    output={"janitor_passed": janitor_passed},
                    actor="task_lifecycle",
                )
            except OSError:
                logger.debug("WAL write failed for %s %s", _wal_dtype, task.id)

        session = orch._find_session_for_task(task.id)
        # Track whether this is the first time we're reaping this session so
        # agent-lifetime metrics are recorded exactly once per agent even when
        # an agent owns multiple tasks that all complete in the same tick.
        _agent_just_reaped = session is not None and session.status != "dead"
        completion_data = collect_completion_data(orch._workdir, session) if session is not None else None
        if session is not None:
            _cache_worktree = orch._spawner.get_worktree_path(session.id)
            if _cache_worktree is not None:
                _cache_diff_lines = _get_git_diff_line_count_in_worktree(_cache_worktree)
            # Quality gates: lint/type/test checks run after janitor, before approval.
            _qg_config = getattr(orch, "_quality_gate_config", None)
            if janitor_passed and _qg_config is not None:
                _worktree_for_gates = orch._spawner.get_worktree_path(session.id)
                _gate_run_dir = _worktree_for_gates if _worktree_for_gates is not None else orch._workdir
                _qg_result = orch._gate_coalescer.run(task, _gate_run_dir, orch._workdir, _qg_config)
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
            # None (the default) means disabled; pass CrossModelVerifierConfig() to enable.
            _cmv_raw = getattr(orch._config, "cross_model_verify", None)
            _cmv_config: CrossModelVerifierConfig = (
                _cmv_raw if isinstance(_cmv_raw, CrossModelVerifierConfig) else CrossModelVerifierConfig(enabled=False)
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

            orch._record_provider_health(session, success=janitor_passed)

            # Bandit feedback: feed quality-cost reward back to the bandit policy
            # so it learns which model performs best for each task context.
            _bandit: Any = getattr(orch, "_bandit_router", None)
            if _bandit is not None:
                _bm = get_collector(orch._workdir / ".sdd" / "metrics").task_metrics.get(task.id)
                _b_cost = _bm.cost_usd if _bm is not None else 0.0
                _b_model = session.model_config.model if session.model_config else "sonnet"
                _b_effort = getattr(session, "effort", "") or ""
                _b_budget = float(getattr(orch._config, "budget_usd", 0.0) or 0.0)
                _bandit.record_outcome(
                    task=task,
                    model=_b_model,
                    effort=_b_effort,
                    cost_usd=_b_cost,
                    quality_score=1.0 if janitor_passed else 0.0,
                    budget_ceiling=_b_budget if _b_budget > 0 else 1.0,
                )
                _bandit.save()

            _skip_merge = False
            if janitor_passed and orch._approval_gate is not None:
                try:
                    _override_mode = None
                    _timeout_s = None

                    wf = getattr(orch._config, "approval_workflow", None)
                    if wf is not None and wf.enabled:
                        risk = getattr(task, "risk_level", "low")
                        mapping = {
                            "low": wf.low_risk,
                            "medium": wf.medium_risk,
                            "high": wf.high_risk,
                            "critical": getattr(wf, "critical_risk", wf.high_risk),
                        }
                        mode_str = mapping.get(risk, "auto")

                        from bernstein.core.approval import ApprovalMode

                        _override_mode = ApprovalMode(mode_str)
                        _timeout_s = float(wf.timeout_hours * 3600)

                        if _override_mode in (ApprovalMode.REVIEW, ApprovalMode.PR):
                            risk_str = risk.upper()
                            orch._notify(
                                event="task.approval_needed",
                                title=f"Approval required ({risk_str} risk): {task.title}",
                                body=f"Task {task.id} requires {mode_str} approval. Timeout: {wf.timeout_hours}h.",
                                task_id=task.id,
                                risk_level=risk,
                            )

                    _approval_result = orch._approval_gate.evaluate(
                        task,
                        session_id=session.id,
                        override_mode=_override_mode,
                        timeout_s=_timeout_s,
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
                            _pr_completion = completion_data or {"files_modified": [], "test_results": {}}
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
                except Exception:
                    logger.exception(
                        "Approval gate failed for task %s -- defaulting to auto-merge",
                        task.id,
                    )
                    _skip_merge = False
            # Reap the agent process and merge the worktree branch, but
            # defer worktree cleanup so the approval gate, PR creation,
            # and merge-result check can still access the worktree
            # directory.  cleanup_worktree is called explicitly below
            # after all post-merge checks complete (BUG-4 fix).
            _merge_result: MergeResult | None = orch._spawner.reap_completed_agent(
                session, skip_merge=_skip_merge, defer_cleanup=True
            )
            if session.status != "dead":
                transition_agent(session, "dead", actor="task_lifecycle", reason="task completed, process reaped")
            logger.info("Agent %s finished task %s, process reaped", session.id, task.id)
            try:
                TeamStateStore(orch._workdir / ".sdd").on_complete(session.id)
            except Exception as _ts_exc:
                logger.debug("Team state on_complete failed: %s", _ts_exc)
            _batch_sessions = getattr(orch, "_batch_sessions", None)
            if isinstance(_batch_sessions, dict) and session.id in _batch_sessions:
                cast("dict[str, AgentSession]", _batch_sessions).pop(session.id, None)
                _release_tasks = getattr(orch, "_release_task_to_session", None)
                if callable(_release_tasks):
                    _release_tasks(session.task_ids)
                _release_files = getattr(orch, "_release_file_ownership", None)
                if callable(_release_files):
                    _release_files(session.id)
            _cache_verified = janitor_passed and session.exit_code == 0 and _cache_diff_lines > 0

            # A/B test outcome recording: persist quality/cost result for this task
            if getattr(orch._config, "ab_test", False):
                _ab_tracker = getattr(orch, "_ab_split_tracker", None)
                if isinstance(_ab_tracker, dict) and task.id in _ab_tracker:
                    _ab_model_map = cast("dict[str, str]", _ab_tracker)
                    try:
                        from bernstein.core.ab_test_results import record_ab_outcome

                        _ab_duration = time.time() - session.spawn_ts
                        record_ab_outcome(
                            orch._workdir,
                            task_id=task.id,
                            task_title=task.title,
                            model=_ab_model_map[task.id],
                            session_id=session.id,
                            tokens_used=session.tokens_used,
                            files_changed=session.files_changed,
                            status="completed" if janitor_passed else "failed",
                            duration_s=_ab_duration,
                        )
                    except Exception as _ab_exc:
                        logger.debug("A/B test outcome recording failed: %s", _ab_exc)

            # Route merge conflicts to a dedicated resolver agent.
            # Check merge result BEFORE closing the task -- a failed merge
            # means the code is missing from main so we must not mark it
            # completed (BUG-5 fix).
            _merge_ok = _merge_result is None or _merge_result.success
            if (
                _merge_result is not None
                and not _merge_result.success
                and _merge_result.conflicting_files
                and not _skip_merge
            ):
                _merge_ok = False
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

            # Move backlog ticket file from open/ to closed/ if it exists.
            # Only close when merge succeeded (or there was no worktree to
            # merge).  A failed merge means code is missing from main --
            # closing the task would hide the problem (BUG-5 fix).
            if janitor_passed and not _skip_merge and _merge_ok:
                _move_backlog_ticket(orch._workdir, task)

                # Transition task to CLOSED on the server (terminal success state)
                try:
                    close_task(orch._client, orch._config.server_url, task.id)
                except Exception as _close_exc:
                    logger.warning("Failed to close task %s: %s", task.id, _close_exc)

                # Close the linked GitHub issue if the task was created from one
                _issue_number = task.metadata.get("issue_number") if task.metadata else None
                if _issue_number:
                    try:
                        from bernstein.core.github import GitHubClient

                        _gh = GitHubClient()
                        _gh.close_issue(int(_issue_number), comment=f"Closed by Bernstein task {task.id}")
                        logger.info("Closed GitHub issue #%s for task %s", _issue_number, task.id)
                    except Exception as exc:
                        logger.warning("Failed to close GitHub issue #%s: %s", _issue_number, exc)

            # Now that the approval gate, PR creation, merge check, and
            # task close are all done, clean up the worktree.  This was
            # deferred during reap_completed_agent so the worktree
            # remained available throughout the post-merge flow (BUG-4 fix).
            orch._spawner.cleanup_worktree(session.id)

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
        orch._cost_tracker.record_cumulative(
            agent_id=_agent_id,
            task_id=task.id,
            model=_model,
            total_input_tokens=_tokens_in,
            total_output_tokens=_tokens_out,
            total_cost_usd=_cost_usd if _cost_usd > 0 else None,
            tenant_id=task.tenant_id,
        )
        try:
            orch._cost_tracker.save(orch._workdir / ".sdd")
        except OSError as exc:
            logger.warning("Failed to persist cost tracker: %s", exc)

        _collector.complete_task(task.id, success=janitor_passed, janitor_passed=janitor_passed, cost_usd=_cost_usd)

        # Record success/failure in convergence guard for error rate tracking
        _convergence_cg = getattr(orch, "_convergence_guard", None)
        if _convergence_cg is not None:
            if janitor_passed:
                _convergence_cg.record_success()
            else:
                _convergence_cg.record_failure()

        try:
            _budget = CompletionBudget(orch._workdir)
            _budget.record_attempt(
                task,
                is_fix=("fix:" in task.title.lower()) or ("judge retry" in task.title.lower()),
                cost_usd=_cost_usd,
            )
        except Exception as exc:
            logger.debug("Completion budget update failed for task %s: %s", task.id, exc)
        if session is not None:
            # complete_agent_task must be called before end_agent so that
            # end_agent() has non-zero task counts and writes the AGENT_SUCCESS
            # metric to the JSONL file.
            _collector.complete_agent_task(session.id, success=janitor_passed)
            _collector.end_agent(session.id)
            try:
                _scorer = EffectivenessScorer(orch._workdir)
                _score = _scorer.score(
                    session,
                    task,
                    _qg_result,
                    completion_data.get("log_summary") if completion_data is not None else None,
                )
                _scorer.record(_score)
                logger.info(
                    "Agent effectiveness: %s grade=%s total=%d",
                    session.id,
                    _score.grade,
                    _score.total,
                )
            except Exception as exc:
                logger.debug("Effectiveness scoring failed for %s: %s", task.id, exc)
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
            _enqueue_paired_test_task(orch, task)
            # Store result in the response cache so future identical tasks can
            # be completed without spawning a new agent.
            if task.result_summary:
                _rc = getattr(orch, "_response_cache", None)
                if _rc is not None:
                    try:
                        _rc.store(
                            _rc.task_key(task.role, task.title, task.description),
                            task.result_summary,
                            verified=_cache_verified,
                            git_diff_lines=_cache_diff_lines,
                            source_task_id=task.id,
                        )
                        _rc.save()
                    except Exception as _rc_store_exc:
                        logger.warning(
                            "Response cache store failed for task %s: %s",
                            task.id,
                            _rc_store_exc,
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
                    task.result_summary or task.title,
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

        # Agent affinity: register downstream tasks to prefer the same agent.
        # When task T1 completes, tasks that depend on T1 become ready. By
        # recording T1's assigned_agent in _agent_affinity we ensure those
        # downstream tasks are batched together and inherit context continuity.
        if janitor_passed and task.assigned_agent:
            _affinity: dict[str, str] | None = getattr(orch, "_agent_affinity", None)
            if _affinity is not None:
                _latest: dict[str, Task] = getattr(orch, "_latest_tasks_by_id", {})
                for _downstream in _latest.values():
                    if task.id in _downstream.depends_on and _downstream.status.value == "open":
                        _affinity[_downstream.id] = task.assigned_agent
                        logger.debug(
                            "agent_affinity: task %s → agent %s (downstream of %s)",
                            _downstream.id,
                            task.assigned_agent,
                            task.id,
                        )


# ---------------------------------------------------------------------------
# Dedicated test-agent slot
# ---------------------------------------------------------------------------


def _enqueue_paired_test_task(orch: Any, completed_task: Task) -> None:
    """Create a paired QA task for completed implementation work.

    Guarded by ``OrchestratorConfig.test_agent`` and idempotent via a marker
    embedded in both title and description.
    """
    config = getattr(orch, "_config", None)
    test_agent_cfg = getattr(config, "test_agent", None)
    if test_agent_cfg is None:
        return
    if not bool(getattr(test_agent_cfg, "always_spawn", False)):
        return
    if str(getattr(test_agent_cfg, "trigger", "")) != "on_task_complete":
        return
    if completed_task.role.lower() in {"qa", "test", "tester"}:
        return

    marker = f"[TEST:{completed_task.id}]"
    if marker in completed_task.title or marker in completed_task.description:
        return

    try:
        existing_resp = orch._client.get(f"{orch._config.server_url}/tasks")
        existing_resp.raise_for_status()
        existing_raw = cast("list[dict[str, Any]]", existing_resp.json())
    except Exception as exc:
        logger.warning("test_agent slot: failed to list tasks for idempotency check: %s", exc)
        return

    for raw in existing_raw:
        title = str(raw.get("title", ""))
        description = str(raw.get("description", ""))
        if marker in title or marker in description:
            return

    payload: dict[str, Any] = {
        "title": f"{marker} Add tests for {completed_task.title[:72]}",
        "description": (
            f"{marker}\n"
            f"Implementation task `{completed_task.id}` completed.\n\n"
            "Write or update tests that validate the implemented behavior, "
            "cover edge cases, and prevent regressions."
        ),
        "role": "qa",
        "priority": completed_task.priority,
        "scope": "small",
        "complexity": "medium",
        "depends_on": [completed_task.id],
        "owned_files": completed_task.owned_files,
        "model": str(getattr(test_agent_cfg, "model", "sonnet")),
        "effort": "high",
    }
    try:
        orch._client.post(f"{orch._config.server_url}/tasks", json=payload).raise_for_status()
        logger.info("test_agent slot: queued paired QA task for %s", completed_task.id)
    except httpx.HTTPError as exc:
        logger.warning("test_agent slot: failed to queue paired QA task for %s: %s", completed_task.id, exc)


# ---------------------------------------------------------------------------
# Private helpers shared with claim_and_spawn_batches
# ---------------------------------------------------------------------------


def _get_changed_files_in_worktree(worktree_path: Path) -> list[str]:
    """Return the list of files changed in a worktree relative to HEAD.

    Args:
        worktree_path: Path to the git worktree.

    Returns:
        List of changed file paths, or empty list on any error.
    """
    import subprocess

    try:
        result = subprocess.run(
            ["git", "diff", "--name-only", "HEAD"],
            cwd=worktree_path,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0:
            return [f for f in result.stdout.splitlines() if f.strip()]
    except Exception as exc:
        logger.debug("_get_changed_files_in_worktree failed for %s: %s", worktree_path, exc)
    return []


def _get_git_diff_line_count_in_worktree(worktree_path: Path) -> int:
    """Return the total tracked diff line count in a worktree.

    Args:
        worktree_path: Path to the git worktree.

    Returns:
        Count of added plus deleted lines from ``git diff --numstat HEAD``.
        Returns 0 on any error or when there are no tracked changes.
    """
    import subprocess

    try:
        result = subprocess.run(
            ["git", "diff", "--numstat", "HEAD"],
            cwd=worktree_path,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            return 0
        total = 0
        for line in result.stdout.splitlines():
            parts = line.split("\t", 2)
            if len(parts) < 2:
                continue
            if parts[0].isdigit():
                total += int(parts[0])
            if parts[1].isdigit():
                total += int(parts[1])
        return total
    except Exception as exc:
        logger.debug("_get_git_diff_line_count_in_worktree failed for %s: %s", worktree_path, exc)
        return 0


# ---------------------------------------------------------------------------
# Backlog ticket lifecycle: move completed tickets to closed/
# ---------------------------------------------------------------------------


def _move_backlog_ticket(workdir: Any, task: Any) -> None:
    """Move a completed task's backlog .md file from open/ to closed/.

    Uses the ``<!-- source: filename.md -->`` tag embedded by sync.py for
    **exact** filename matching.  Falls back to exact normalised-title match
    (never substring).  This prevents accidental closure of unrelated tickets.

    Args:
        workdir: Project root (Path-like).
        task: Completed Task object.
    """
    from pathlib import Path

    _log = logging.getLogger(__name__)
    open_dir = Path(workdir) / ".sdd" / "backlog" / "open"
    closed_dir = Path(workdir) / ".sdd" / "backlog" / "closed"
    if not open_dir.exists():
        return
    closed_dir.mkdir(parents=True, exist_ok=True)

    # --- Strategy 1: exact filename from <!-- source: ... --> tag ---
    source_match = re.search(r"<!--\s*source:\s*(\S+\.md)\s*-->", getattr(task, "description", "") or "")
    if source_match:
        source_file = open_dir / source_match.group(1)
        if source_file.exists():
            try:
                source_file.rename(closed_dir / source_file.name)
                _log.info(
                    "Moved ticket %s to closed/ (exact source match, task: %s)", source_file.name, task.title[:50]
                )
            except OSError:
                pass
            return

    # --- Strategy 2: exact normalised-title match (no substring!) ---
    title_slug = re.sub(r"[^a-z0-9]+", "-", task.title.lower()).strip("-")
    for md_file in [*open_dir.glob("*.yaml"), *open_dir.glob("*.md")]:
        # Parse the ticket heading and normalise it
        try:
            text = md_file.read_text(encoding="utf-8")
        except OSError:
            continue
        for line in text.splitlines():
            if line.startswith("# "):
                heading = re.sub(r"^[0-9a-fA-F]+\s*[—:\-]\s*", "", line[2:].strip())
                heading_slug = re.sub(r"[^a-z0-9]+", "-", heading.lower()).strip("-")
                if heading_slug == title_slug:
                    try:
                        md_file.rename(closed_dir / md_file.name)
                        _log.info("Moved ticket %s to closed/ (title match, task: %s)", md_file.name, task.title[:50])
                    except OSError:
                        pass
                    return
                break  # only check first heading


# ---------------------------------------------------------------------------
# Priority decay for old unclaimed tasks
# ---------------------------------------------------------------------------

#: Hours before an open task is deprioritized.
PRIORITY_DECAY_THRESHOLD_HOURS = 24

#: Minimum priority (tasks won't go below this).
MIN_PRIORITY = 3


def deprioritize_old_unclaimed_tasks(
    orch: Any,
    threshold_hours: int = PRIORITY_DECAY_THRESHOLD_HOURS,
    min_priority: int = MIN_PRIORITY,
) -> int:
    """Deprioritize tasks that have been open for too long without being claimed.

    Called during janitor tick. Tasks open for > threshold_hours without being
    claimed have their priority decreased by 1 (min priority floor).

    Args:
        orch: Orchestrator instance.
        threshold_hours: Hours before deprioritization.
        min_priority: Minimum priority value.

    Returns:
        Count of tasks deprioritized.
    """

    now = time.time()
    threshold_seconds = threshold_hours * 3600
    deprioritized_count = 0

    for task in orch._store.list_tasks():
        if task.status != TaskStatus.OPEN:
            continue

        # Check if task has been open too long
        age_seconds = now - task.created_at
        if age_seconds < threshold_seconds:
            continue

        # Check if task was ever claimed (has agent history)
        # If it was claimed and returned to open, don't deprioritize
        # For simplicity, we deprioritize all old open tasks

        old_priority = task.priority
        new_priority = min(min_priority, old_priority + 1)

        if new_priority > old_priority:
            # Update task priority (optimistic locking)
            try:
                orch._store.update_task_priority(task.id, new_priority, task.version)
                deprioritized_count += 1
                logger.info(
                    "Task %s deprioritized after %.0f h unclaimed (%d → %d)",
                    task.id,
                    age_seconds / 3600,
                    old_priority,
                    new_priority,
                )
            except Exception as exc:
                logger.debug("Failed to deprioritize task %s: %s", task.id, exc)

    return deprioritized_count


# ---------------------------------------------------------------------------
# Permission denied hooks for retry hints (T570)
# ---------------------------------------------------------------------------


def handle_permission_denied_error(error_message: str, task_id: str, role: str, retry_count: int) -> dict[str, Any]:
    """Handle permission denied errors with retry hints."""
    from bernstein.core.worker import get_permission_hint

    hint = get_permission_hint(error_message)

    if hint:
        logger.warning(f"Permission denied for task {task_id} ({role}): {error_message}\nHint: {hint}")

        # Determine if we should retry
        should_retry = retry_count < 2  # Max 2 retries for permission issues

        return {
            "permission_denied": True,
            "error_message": error_message,
            "hint": hint,
            "should_retry": should_retry,
            "retry_count": retry_count,
            "max_retries": 2,
        }
    else:
        logger.warning(f"Permission denied for task {task_id} ({role}): {error_message}")

        return {
            "permission_denied": True,
            "error_message": error_message,
            "hint": None,
            "should_retry": False,
            "retry_count": retry_count,
            "max_retries": 2,
        }
