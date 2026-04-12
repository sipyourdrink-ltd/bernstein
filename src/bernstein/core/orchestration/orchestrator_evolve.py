"""Orchestrator evolution mode: self-improvement cycles, ruff checks, test runs.

Extracted from orchestrator.py as part of ORCH-009 decomposition.
Functions here operate on an Orchestrator instance passed as the first
argument, keeping the Orchestrator class as the public facade.
"""

from __future__ import annotations

import concurrent.futures
import contextlib
import json
import logging
import re
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import httpx

from bernstein.core.models import TaskType
from bernstein.core.orchestration.evolution import UpgradeStatus
from bernstein.core.platform_compat import kill_process_group

if TYPE_CHECKING:
    from bernstein.core.orchestration.tick_pipeline import (
        RuffViolation,
        TestResults,
    )

logger = logging.getLogger(__name__)

# Priority rotation for evolve mode -- each cycle emphasizes a different area
_EVOLVE_FOCUS_AREAS: list[str] = [
    "new_features",
    "user_interface",
    "test_coverage",
    "code_quality",
    "performance",
    "documentation",
]

_REPLENISH_COOLDOWN_S: float = 60.0
_REPLENISH_MAX_TASKS: int = 5


def check_evolve(orch: Any, result: Any, tasks_by_status: dict[str, list[Any]]) -> None:
    """If evolve mode is on and all tasks are done, trigger a new cycle.

    Args:
        orch: The orchestrator instance.
        result: Current tick result (mutated in place).
        tasks_by_status: Pre-fetched task snapshot keyed by status string.
    """
    from bernstein.evolution.governance import GovernanceEntry, ProjectContext

    evolve_path = orch._workdir / ".sdd" / "runtime" / "evolve.json"
    if not evolve_path.exists():
        return

    try:
        evolve_cfg = json.loads(evolve_path.read_text())
    except (OSError, json.JSONDecodeError):
        return

    if not evolve_cfg.get("enabled"):
        return

    # Only trigger when idle: no open/claimed tasks, no alive agents
    open_tasks = tasks_by_status.get("open", [])
    claimed_tasks = tasks_by_status.get("claimed", [])
    alive = sum(1 for a in orch._agents.values() if a.status != "dead")
    if open_tasks or claimed_tasks or alive > 0:
        return  # Still working

    # Check cycle limits
    cycle_count = evolve_cfg.get("_cycle_count", 0)
    max_cycles = evolve_cfg.get("max_cycles", 0)
    if max_cycles > 0 and cycle_count >= max_cycles:
        logger.info("Evolve: max cycles (%d) reached, stopping", max_cycles)
        return

    # Check budget cap
    budget_usd = evolve_cfg.get("budget_usd", 0)
    spent_usd = evolve_cfg.get("_spent_usd", 0.0)
    if budget_usd > 0 and spent_usd >= budget_usd:
        logger.info("Evolve: budget cap ($%.2f) reached, stopping", budget_usd)
        return

    # Diminishing returns backoff
    consecutive_empty = evolve_cfg.get("_consecutive_empty", 0)
    backoff_factor = min(2**consecutive_empty, 8) if consecutive_empty >= 3 else 1

    last_cycle_ts = evolve_cfg.get("_last_cycle_ts", 0)
    base_interval = evolve_cfg.get("interval_s", 300)
    effective_interval = base_interval * backoff_factor
    if time.time() - last_cycle_ts < effective_interval:
        return

    cycle_number = cycle_count + 1
    cycle_start = time.time()
    logger.info(
        "Evolve: triggering cycle %d (backoff=%dx, interval=%ds)",
        cycle_number,
        backoff_factor,
        effective_interval,
    )

    # Step 1: ANALYZE
    tasks_completed = len(tasks_by_status.get("done", []))
    tasks_failed = len(tasks_by_status.get("failed", []))

    # Step 2: VERIFY
    test_info = orch._evolve_run_tests()

    # Step 3: COMMIT
    committed = orch._evolve_auto_commit()

    # Step 3b: GOVERN
    # _governor is always non-None here because _check_evolve only runs
    # when evolve_mode is enabled, and we initialize the governor in that case.
    assert orch._governor is not None, "AdaptiveGovernor must be initialized in evolve mode"
    weights_before = orch._governor.get_current_weights()
    test_pass_rate = test_info.get("passed", 0) / max(test_info.get("passed", 0) + test_info.get("failed", 0), 1)
    gov_context = ProjectContext(
        cycle_number=cycle_number,
        test_pass_rate=test_pass_rate,
        lint_violations=evolve_cfg.get("_lint_violations", 0),
        security_issues_last_5_cycles=evolve_cfg.get("_security_issues", 0),
        codebase_size_files=evolve_cfg.get("_codebase_files", 0),
        consecutive_empty_cycles=consecutive_empty,
    )
    weights_after, weight_reason = orch._governor.adjust_weights(weights_before, gov_context)
    orch._governor.persist_weights(weights_after, reason=weight_reason)
    orch._governor.log_decision(
        GovernanceEntry(
            cycle=cycle_number,
            timestamp=datetime.now(UTC).isoformat(),
            weights_before=weights_before.to_dict(),
            weights_after=weights_after.to_dict(),
            weight_change_reason=weight_reason,
            proposals_evaluated=tasks_completed + tasks_failed,
            proposals_applied=tasks_completed,
            risk_scores=orch._last_cycle_risk_scores,
            outcome_metrics={
                "test_pass_rate": test_pass_rate,
                "committed": 1.0 if committed else 0.0,
            },
        )
    )
    logger.info(
        "Evolve: governance cycle %d -- weights adjusted (%s)",
        cycle_number,
        weight_reason,
    )

    # Step 4: PLAN
    focus_areas: list[str] = _EVOLVE_FOCUS_AREAS
    focus_idx: int = cycle_count % len(focus_areas)
    focus: str = str(focus_areas[focus_idx])
    orch._evolve_spawn_manager(
        cycle_number=cycle_number,
        focus_area=focus,
        test_summary=test_info.get("summary", ""),
    )

    # Track diminishing returns
    produced_changes = committed or tasks_completed > 0
    if produced_changes:
        evolve_cfg["_consecutive_empty"] = 0
    else:
        evolve_cfg["_consecutive_empty"] = consecutive_empty + 1

    # Update state
    now = time.time()
    evolve_cfg["_cycle_count"] = cycle_number
    evolve_cfg["_last_cycle_ts"] = now
    with contextlib.suppress(OSError):
        evolve_path.write_text(json.dumps(evolve_cfg))

    # Log cycle metrics
    orch._log_evolve_cycle(
        cycle_number,
        now,
        {
            "focus_area": focus,
            "tasks_completed": tasks_completed,
            "tasks_failed": tasks_failed,
            "tests_passed": test_info.get("passed", 0),
            "tests_failed": test_info.get("failed", 0),
            "commits_made": 1 if committed else 0,
            "backoff_factor": backoff_factor,
            "consecutive_empty": evolve_cfg.get("_consecutive_empty", 0),
            "duration_s": round(now - cycle_start, 2),
        },
    )

    orch._post_bulletin(
        "status",
        f"evolve cycle {cycle_number} complete: focus={focus}, completed={tasks_completed}, committed={committed}",
    )


def run_ruff_check(orch: Any) -> list[RuffViolation]:
    """Run ruff check and return parsed violations (runs in a background thread).

    Args:
        orch: The orchestrator instance.

    Returns:
        List of ruff violation dicts.
    """
    import subprocess

    proc = subprocess.Popen(
        ["uv", "run", "ruff", "check", ".", "--output-format", "json"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=orch._workdir,
        start_new_session=True,
    )
    try:
        stdout, _ = proc.communicate(timeout=60)
    except subprocess.TimeoutExpired:
        kill_process_group(proc.pid, sig=9)
        proc.wait()
        return []
    return json.loads(stdout) if stdout.strip() else []


def create_ruff_tasks(orch: Any, violations: list[RuffViolation]) -> None:
    """Create backlog tasks from ruff violations.

    Args:
        orch: The orchestrator instance.
        violations: List of ruff violation dicts.
    """
    if not violations:
        logger.debug("Replenish: no ruff violations found, backlog is clean")
        return

    by_rule: dict[str, RuffViolation] = {}
    for v in violations:
        code = (v.get("code") or "unknown").strip()
        if code not in by_rule:
            by_rule[code] = v

    base = orch._config.server_url
    created = 0
    for code, v in by_rule.items():
        if created >= _REPLENISH_MAX_TASKS:
            break
        filename = v.get("filename", "")
        message = v.get("message", "")
        row = v.get("location", {}).get("row", "?")
        task_payload = {
            "title": f"Fix ruff violation {code}",
            "description": (
                f"Fix all occurrences of ruff rule {code}.\n"
                f"Example: {filename}:{row} -- {message}\n"
                f"Run `uv run ruff check . --select {code}` to find all instances."
            ),
            "role": "backend",
            "priority": 3,
            "model": "sonnet",
            "effort": "low",
        }
        try:
            resp = orch._client.post(f"{base}/tasks", json=task_payload)
            resp.raise_for_status()
            created += 1
            logger.info("Replenish: created task for ruff rule %s", code)
        except httpx.HTTPError as exc:
            logger.warning("Replenish: failed to create task for %s: %s", code, exc)

    if created:
        logger.info("Replenish: created %d lint-fix task(s)", created)


def replenish_backlog(orch: Any, result: Any) -> None:
    """Create fix tasks from ruff lint violations when evolve mode is idle.

    Args:
        orch: The orchestrator instance.
        result: The TickResult from the current tick.
    """
    if not orch._config.evolve_mode:
        return
    if result.open_tasks > 0:
        return

    # Harvest a completed ruff future
    if orch._pending_ruff_future is not None:
        if not orch._pending_ruff_future.done():
            return  # still running; skip this tick
        try:
            violations: list[RuffViolation] = orch._pending_ruff_future.result()
        except (concurrent.futures.CancelledError, RuntimeError) as exc:
            logger.warning("Replenish: ruff check failed: %s", exc)
            orch._pending_ruff_future = None
            return
        orch._pending_ruff_future = None
        create_ruff_tasks(orch, violations)
        return

    # Check cooldown before submitting a new run
    now = time.time()
    if now - orch._last_replenish_ts < _REPLENISH_COOLDOWN_S:
        return

    orch._last_replenish_ts = now
    orch._pending_ruff_future = orch._executor.submit(run_ruff_check, orch)
    logger.debug("Replenish: ruff check submitted to background thread")


def run_pytest(orch: Any) -> TestResults:
    """Run pytest and return parsed results (runs in a background thread).

    Args:
        orch: The orchestrator instance.

    Returns:
        TestResults dict with passed, failed, summary.
    """
    import subprocess

    info: TestResults = {"passed": 0, "failed": 0, "summary": ""}
    proc = subprocess.Popen(
        ["uv", "run", "pytest", "tests/", "-x", "-q", "--tb=line"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=orch._workdir,
        start_new_session=True,
    )
    try:
        stdout, stderr = proc.communicate(timeout=120)
    except subprocess.TimeoutExpired:
        if not kill_process_group(proc.pid, sig=9):
            proc.kill()
        proc.wait()
        info["summary"] = "pytest timed out after 120s"
        logger.warning("Background pytest timed out, killed process group")
        return info

    output = stdout + stderr
    info["summary"] = output.strip().splitlines()[-1] if output.strip() else ""
    match = re.search(r"(\d+) passed\b", output)
    if match:
        info["passed"] = int(match.group(1))
    match = re.search(r"(\d+) failed\b", output)
    if match:
        info["failed"] = int(match.group(1))
    return info


def evolve_run_tests(orch: Any) -> TestResults:
    """Return test results from a background pytest run.

    Args:
        orch: The orchestrator instance.

    Returns:
        TestResults dict with passed, failed, summary.
    """
    info: TestResults = {"passed": 0, "failed": 0, "summary": ""}

    if orch._pending_test_future is not None:
        if not orch._pending_test_future.done():
            return info
        try:
            info = orch._pending_test_future.result()
        except (concurrent.futures.CancelledError, RuntimeError) as exc:
            logger.warning("Evolve: test run failed: %s", exc)
            info["summary"] = f"test run error: {exc}"
        orch._pending_test_future = None
        return info

    orch._pending_test_future = orch._executor.submit(run_pytest, orch)
    return info


def generate_evolve_commit_msg(staged_files: list[str]) -> str:
    """Build a short, descriptive commit message from the list of staged files.

    Args:
        staged_files: List of staged file paths.

    Returns:
        A commit message string.
    """
    if not staged_files:
        return "Evolve: housekeeping"

    LABEL_RULES: list[tuple[str, str]] = [
        ("src/bernstein/cli/dashboard", "improve dashboard"),
        ("src/bernstein/cli/main", "update CLI"),
        ("src/bernstein/cli/cost", "add cost tracking"),
        ("src/bernstein/cli/", "update CLI"),
        ("src/bernstein/core/orchestrator", "fix orchestrator"),
        ("src/bernstein/core/server", "fix server"),
        ("src/bernstein/core/models", "extend models"),
        ("src/bernstein/core/spawner", "fix spawner"),
        ("src/bernstein/core/", "update core"),
        ("src/bernstein/adapters/", "refactor adapters"),
        ("src/bernstein/evolution/", "tune evolution"),
        ("src/bernstein/agents/", "update agents"),
        ("tests/", "update tests"),
        ("docs/", "update docs"),
        ("README", "update README"),
        ("CONTRIBUTING", "update CONTRIBUTING"),
        (".sdd/backlog/", "add backlog tasks"),
    ]

    seen: set[str] = set()
    labels: list[str] = []
    for path in staged_files:
        for prefix, label in LABEL_RULES:
            if prefix in path and label not in seen:
                seen.add(label)
                labels.append(label)
                break

    if not labels:
        first = staged_files[0].split("/")[-1]
        labels = [f"update {first}"]

    summary = "; ".join(labels[:3])
    return f"Evolve: {summary}"


def evolve_auto_commit(orch: Any) -> bool:
    """Auto-commit and push any uncommitted changes from the last cycle.

    Args:
        orch: The orchestrator instance.

    Returns:
        True if changes were committed and pushed.
    """
    import subprocess

    from bernstein.core.git_ops import (
        checkout_discard,
        conventional_commit,
        safe_push,
        stage_all_except,
        status_porcelain,
    )

    try:
        changed = status_porcelain(orch._workdir)
        if not changed:
            return False

        stage_all_except(orch._workdir, exclude=[".sdd/runtime/", ".sdd/metrics/"])

        test_result = subprocess.run(
            ["uv", "run", "pytest", "tests/", "-x", "-q", "--tb=line"],
            capture_output=True,
            text=True,
            cwd=orch._workdir,
            timeout=300,
        )
        if test_result.returncode != 0:
            logger.warning("Evolve: tests failed, rolling back changes")
            checkout_discard(orch._workdir)
            return False

        result = conventional_commit(orch._workdir, evolve=True)
        if not result.ok:
            logger.warning("Evolve: commit failed: %s", result.stderr)
            return False

        safe_push(orch._workdir, "main")
        logger.info("Evolve: auto-committed and pushed changes")

        if "src/bernstein/" in changed:
            logger.info("Evolve: own source code changed, signaling restart")
            restart_flag = orch._workdir / ".sdd" / "runtime" / "restart_requested"
            restart_flag.parent.mkdir(parents=True, exist_ok=True)
            restart_flag.write_text(str(time.time()))

        return True

    except (subprocess.TimeoutExpired, OSError) as exc:
        logger.warning("Evolve: auto-commit failed: %s", exc)
        return False


def evolve_spawn_manager(
    orch: Any,
    cycle_number: int = 0,
    focus_area: str = "new_features",
    test_summary: str = "",
) -> None:
    """Spawn a manager agent to analyze the codebase and create new tasks.

    Args:
        orch: The orchestrator instance.
        cycle_number: Current evolution cycle number.
        focus_area: The focus area for this cycle.
        test_summary: Summary of the latest test run.
    """
    base = orch._config.server_url

    research_context = ""
    try:
        from bernstein.core.researcher import format_research_context, run_research_sync

        report = run_research_sync(orch._workdir)
        research_context = format_research_context(report)
        if research_context:
            logger.info("Evolve: research produced %d bytes of context", len(research_context))
    except Exception as exc:
        logger.debug("Evolve: research unavailable: %s", exc)

    focus_instructions = {
        "new_features": "Focus on missing features that block real usage.",
        "user_interface": (
            "Focus on the CLI dashboard and user-facing experience. "
            "Improve the Textual dashboard (src/bernstein/cli/dashboard.py): "
            "better live metrics display, clearer task status, more useful panels. "
            "Also improve CLI output quality and error messages."
        ),
        "test_coverage": "Focus on test gaps and missing edge-case coverage.",
        "code_quality": "Focus on code smells, type safety, and refactoring.",
        "performance": "Focus on performance bottlenecks and efficiency.",
        "documentation": "Focus on missing docs that block contributors.",
    }
    focus_text = focus_instructions.get(focus_area, "Focus on high-impact improvements.")

    description = (
        f"You are a PRODUCT DIRECTOR in EVOLVE mode (cycle {cycle_number}). "
        "Think strategically: what would make this project genuinely useful "
        "to developers? What do competitors lack? What's the shortest path "
        "to a feature that gets people excited?\n\n"
        "Create tasks for specialist agents to implement. "
        "You plan, they code.\n\n"
        f"## This cycle's focus: {focus_area.replace('_', ' ')}\n"
        f"{focus_text}\n\n"
        + (f"## Current test state\n```\n{test_summary}\n```\n\n" if test_summary else "")
        + "## Rules (from self-evolving systems research)\n"
        "- NEVER create tasks that are cosmetic, trivial, or busy-work\n"
        "- Each task must have a measurable outcome (test passes, "
        "benchmark improves, bug is fixed)\n"
        "- Prefer config/prompt changes over code changes (cheaper, safer)\n"
        "- If tests already pass at 100%, focus on functionality, not more tests\n"
        "- If architecture is clean, focus on features users actually need\n"
        "- Create 3-5 tasks MAX. Quality over quantity.\n\n"
        "## Prioritization\n"
        "1. Bugs and broken functionality (P1)\n"
        "2. Missing features that block real usage (P1)\n"
        "3. Performance and reliability (P2)\n"
        "4. Code quality and test gaps (P2)\n"
        "5. Documentation (P3 -- only if truly missing)\n\n"
        "## Process\n"
        "1. Run `uv run python scripts/run_tests.py -x` to see current test state\n"
        "2. Read key files to understand architecture\n"
        "3. Identify 3-5 high-impact improvements\n"
        "4. Create tasks via HTTP. YOU decide model and effort per task:\n"
        f"   curl -X POST {base}/tasks -H 'Content-Type: application/json' \\\n"
        '   -d \'{"title": "...", "description": "...", '
        '"role": "backend", "priority": 2, '
        '"model": "sonnet", "effort": "high"}\'\n\n'
        "## Model/effort selection (you decide per task)\n"
        '- model: "opus" (deep reasoning, slow) or "sonnet" (fast, default)\n'
        '- effort: "max" (100 turns), "high" (50), "medium" (30), "low" (15)\n'
        "- Use sonnet/high for most implementation tasks (fast)\n"
        "- Use opus/max ONLY for complex architecture or security reviews\n"
        "- Use sonnet/low for simple fixes, typos, config changes\n\n"
        "## Task size -- KEEP THEM SMALL\n"
        "Each task MUST be completable in ONE file change, under 10 minutes.\n"
        "BAD: 'Implement entire web research module'\n"
        "GOOD: 'Add Tavily search function to researcher.py'\n"
        "GOOD: 'Add --evolve flag handling to cli/main.py'\n"
        "Break big features into 3-5 atomic file-level tasks.\n\n"
        "## README\n"
        "Every 3rd cycle, create a task to update README.md with:\n"
        "- Current feature state, correct CLI usage, accurate test count.\n\n"
        "5. Then exit.\n\n"
        "IMPORTANT: Do NOT implement changes yourself. Only create tasks."
    )

    if research_context:
        description += research_context

    task_body = {
        "title": f"Evolve cycle {cycle_number}: {focus_area.replace('_', ' ')}",
        "description": description,
        "role": "manager",
        "priority": 1,
        "scope": "medium",
        "complexity": "medium",
    }

    try:
        resp = orch._client.post(f"{base}/tasks", json=task_body)
        resp.raise_for_status()
        task_id = resp.json().get("id", "?")
        logger.info("Evolve: created manager task %s (focus=%s)", task_id, focus_area)
    except httpx.HTTPError as exc:
        logger.error("Evolve: failed to create manager task: %s", exc)


def log_evolve_cycle(
    orch: Any,
    cycle_number: int,
    timestamp: float,
    metrics: dict[str, Any] | None = None,
) -> None:
    """Append an entry to the evolve_cycles.jsonl log.

    Args:
        orch: The orchestrator instance.
        cycle_number: Current cycle number.
        timestamp: Cycle timestamp.
        metrics: Optional cycle metrics dict.
    """
    metrics_dir = orch._workdir / ".sdd" / "metrics"
    metrics_dir.mkdir(parents=True, exist_ok=True)
    log_path = metrics_dir / "evolve_cycles.jsonl"
    entry: dict[str, Any] = {
        "cycle": cycle_number,
        "timestamp": timestamp,
        "iso_time": datetime.fromtimestamp(timestamp, tz=UTC).isoformat(),
        "tick": orch._tick_count,
    }
    if metrics:
        entry.update(metrics)
    try:
        with log_path.open("a") as f:
            f.write(json.dumps(entry) + "\n")
    except OSError as exc:
        logger.warning("Evolve: failed to write cycle log: %s", exc)


def make_evolution_loop(orch: Any, **kwargs: Any) -> Any:
    """Create an EvolutionLoop wired to this orchestrator's AdaptiveGovernor.

    Passes the orchestrator's governor so the evolution loop shares the
    same weight history and governance log as the orchestrator's evolve
    cycles.  Any extra keyword arguments are forwarded to ``EvolutionLoop``.

    Args:
        orch: The orchestrator instance.
        **kwargs: Additional keyword arguments for EvolutionLoop.

    Returns:
        A fully-wired ``EvolutionLoop`` instance.
    """
    from bernstein.evolution.loop import EvolutionLoop

    return EvolutionLoop(
        state_dir=orch._workdir / ".sdd",
        repo_root=orch._workdir,
        governor=orch._governor,
        **kwargs,
    )


def run_evolution_cycle(orch: Any, result: Any) -> None:
    """Run an evolution analysis cycle and create upgrade tasks from proposals.

    Args:
        orch: The orchestrator instance.
        result: The TickResult (mutated in place with errors).
    """
    assert orch._evolution is not None
    try:
        proposals = orch._evolution.run_analysis_cycle()

        # Score each proposal with Strategic Risk Score before routing
        cycle_risk_scores: list[float] = []
        for proposal in proposals:
            target_files = proposal.risk_assessment.affected_components
            # Estimate diff size from description length (heuristic)
            diff_estimate = max(len(proposal.proposed_change) // 10, 10)
            risk_score = orch._risk_scorer.score_proposal(
                target_files=target_files,
                diff_size=diff_estimate,
                test_coverage_delta=0.0,  # unknown pre-execution
            )
            cycle_risk_scores.append(risk_score.composite_risk)
            if orch._risk_scorer.is_high_risk(risk_score):
                logger.info(
                    "Proposal %s (%s) flagged high-risk (%.2f) — routing to sandbox",
                    proposal.id,
                    proposal.title,
                    risk_score.composite_risk,
                )
            else:
                logger.info(
                    "Proposal %s (%s) low-risk (%.2f) — fast-tracking",
                    proposal.id,
                    proposal.title,
                    risk_score.composite_risk,
                )
        orch._last_cycle_risk_scores = cycle_risk_scores

        # Persist pending proposals
        persist_pending_proposals(orch)

        # Execute approved proposals; rollback on failure
        executed = orch._evolution.execute_pending_upgrades()
        for proposal in executed:
            logger.info(
                "Applied upgrade %s: %s (status=%s)",
                proposal.id,
                proposal.title,
                proposal.status.value,
            )

        if not proposals:
            return

        _task_eligible_statuses = {UpgradeStatus.PENDING, UpgradeStatus.APPROVED}
        base = orch._config.server_url
        for proposal in proposals:
            if proposal.status not in _task_eligible_statuses:
                continue
            try:
                # Score for task priority routing
                target_files = proposal.risk_assessment.affected_components
                diff_estimate = max(len(proposal.proposed_change) // 10, 10)
                risk_score = orch._risk_scorer.score_proposal(
                    target_files=target_files,
                    diff_size=diff_estimate,
                    test_coverage_delta=0.0,
                )
                is_high = orch._risk_scorer.is_high_risk(risk_score)
                task_body = {
                    "title": f"Upgrade: {proposal.title}",
                    "description": proposal.description,
                    "role": "backend",
                    "priority": 1 if is_high else 2,
                    "scope": "large" if is_high else "medium",
                    "complexity": "high" if is_high else "medium",
                    "estimated_minutes": 60 if is_high else 30,
                    "task_type": TaskType.UPGRADE_PROPOSAL.value,
                }
                resp = orch._client.post(f"{base}/tasks", json=task_body)
                resp.raise_for_status()
                logger.info(
                    "Created upgrade task for proposal %s: %s (risk=%.2f)",
                    proposal.id,
                    proposal.title,
                    risk_score.composite_risk,
                )
            except httpx.HTTPError as exc:
                logger.warning(
                    "Failed to create upgrade task for proposal %s: %s",
                    proposal.id,
                    exc,
                )
                result.errors.append(f"evolution_task: {exc}")
    except (OSError, ValueError, RuntimeError) as exc:
        logger.error("Evolution analysis cycle failed: %s", exc)
        result.errors.append(f"evolution: {exc}")


def persist_pending_proposals(orch: Any) -> None:
    """Write pending upgrade proposals to .sdd/upgrades/pending.json.

    Args:
        orch: The orchestrator instance.
    """
    if orch._evolution is None:
        return
    upgrades_dir = orch._workdir / ".sdd" / "upgrades"
    upgrades_dir.mkdir(parents=True, exist_ok=True)
    pending_path = upgrades_dir / "pending.json"
    pending = orch._evolution.get_pending_upgrades()
    data = [
        {
            "id": p.id,
            "title": p.title,
            "category": p.category.value,
            "description": p.description,
            "status": p.status.value,
            "confidence": p.confidence,
            "created_at": p.created_at,
        }
        for p in pending
    ]
    pending_path.write_text(json.dumps(data, indent=2))
