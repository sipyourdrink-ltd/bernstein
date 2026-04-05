"""A/B test runner: run the same task with two models and compare results.

Creates two identical tasks pinned to different models, waits for both to
complete, then builds a structured comparison report.
"""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass
from typing import Any, Literal

import httpx

logger = logging.getLogger(__name__)

_DEFAULT_TIMEOUT_SECONDS = 1800  # 30 minutes
_POLL_INTERVAL_SECONDS = 5


# ---------------------------------------------------------------------------
# Config & result dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ABTestConfig:
    """Configuration for a single A/B test run.

    Attributes:
        task_description: Goal/description for both tasks.
        model_a: First model name (e.g. "opus").
        model_b: Second model name (e.g. "sonnet").
        role: Agent role to assign (default "backend").
        scope: Task scope — small, medium, or large.
        timeout_seconds: Max wait time before declaring the test timed-out.
    """

    task_description: str
    model_a: str
    model_b: str
    role: str = "backend"
    scope: str = "medium"
    timeout_seconds: int = _DEFAULT_TIMEOUT_SECONDS


@dataclass(frozen=True)
class ABTestResult:
    """Per-model outcome from an A/B test.

    Attributes:
        model: Model name used for this variant.
        variant: Which side of the test — "a" or "b".
        task_id: Task ID on the server.
        duration_seconds: Wall-clock seconds from creation to completion.
        cost_usd: Estimated cost reported by the server (0.0 if unavailable).
        input_tokens: Input token count (0 if unavailable).
        output_tokens: Output token count (0 if unavailable).
        passed: Whether the task completed successfully (not failed).
        quality_passed: Whether the task passed quality gates / janitor.
        status: Final task status string.
    """

    model: str
    variant: Literal["a", "b"]
    task_id: str
    duration_seconds: float
    cost_usd: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    passed: bool = False
    quality_passed: bool = False
    status: str = "unknown"


@dataclass(frozen=True)
class ABTestReport:
    """Comparison report for an A/B test.

    Attributes:
        test_id: Unique identifier for this A/B test run.
        config: The configuration that produced this report.
        result_a: Metrics for model A.
        result_b: Metrics for model B.
        winner: Which variant won ("a", "b", or "tie").
        reason: Human-readable explanation of why that variant won.
        timed_out: True if the test hit the timeout before both tasks finished.
    """

    test_id: str
    config: ABTestConfig
    result_a: ABTestResult
    result_b: ABTestResult
    winner: Literal["a", "b", "tie"]
    reason: str
    timed_out: bool = False

    def to_markdown(self) -> str:
        """Render the report as a Markdown comparison table.

        Returns:
            Markdown string suitable for terminal or file output.
        """
        ra = self.result_a
        rb = self.result_b
        lines: list[str] = [
            f"# A/B Test Report — {self.test_id}",
            "",
            f"**Task:** {self.config.task_description}",
            f"**Role:** {self.config.role}  **Scope:** {self.config.scope}",
            "",
            f"| Metric | Model A ({ra.model}) | Model B ({rb.model}) |",
            "|--------|{a}|{b}|".format(a="-" * (len(ra.model) + 12), b="-" * (len(rb.model) + 12)),
            f"| Status | {ra.status} | {rb.status} |",
            f"| Passed | {ra.passed} | {rb.passed} |",
            f"| Quality | {ra.quality_passed} | {rb.quality_passed} |",
            f"| Duration | {ra.duration_seconds:.1f}s | {rb.duration_seconds:.1f}s |",
            f"| Cost | ${ra.cost_usd:.4f} | ${rb.cost_usd:.4f} |",
            f"| Input tokens | {ra.input_tokens:,} | {rb.input_tokens:,} |",
            f"| Output tokens | {ra.output_tokens:,} | {rb.output_tokens:,} |",
            "",
            f"**Winner:** {self.winner.upper()} ({self._winner_model()}) — {self.reason}",
        ]
        if self.timed_out:
            lines.append("\n*Test timed out before both tasks completed.*")
        return "\n".join(lines)

    def _winner_model(self) -> str:
        if self.winner == "a":
            return self.result_a.model
        if self.winner == "b":
            return self.result_b.model
        return "tie"


# ---------------------------------------------------------------------------
# Winner determination
# ---------------------------------------------------------------------------


def determine_winner(
    result_a: ABTestResult,
    result_b: ABTestResult,
) -> tuple[Literal["a", "b", "tie"], str]:
    """Decide which variant wins.

    Priority order:
      1. Quality — both must pass; if only one does, it wins.
      2. Cost — lower cost wins (within 5% tolerance, move to speed).
      3. Speed — lower duration wins.

    Args:
        result_a: Metrics for variant A.
        result_b: Metrics for variant B.

    Returns:
        Tuple of (winner literal, human-readable reason).
    """
    # 1. Quality gate
    if result_a.passed and not result_b.passed:
        return "a", f"{result_a.model} passed while {result_b.model} failed"
    if result_b.passed and not result_a.passed:
        return "b", f"{result_b.model} passed while {result_a.model} failed"
    if not result_a.passed and not result_b.passed:
        return "tie", "both models failed"

    # Quality sub-check
    if result_a.quality_passed and not result_b.quality_passed:
        return "a", f"{result_a.model} passed quality gates; {result_b.model} did not"
    if result_b.quality_passed and not result_a.quality_passed:
        return "b", f"{result_b.model} passed quality gates; {result_a.model} did not"

    # 2. Cost (5% tolerance band)
    if result_a.cost_usd > 0 and result_b.cost_usd > 0:
        cost_ratio = result_a.cost_usd / result_b.cost_usd if result_b.cost_usd else 1.0
        if cost_ratio < 0.95:
            return "a", f"{result_a.model} was cheaper (${result_a.cost_usd:.4f} vs ${result_b.cost_usd:.4f})"
        if cost_ratio > 1.05:
            return "b", f"{result_b.model} was cheaper (${result_b.cost_usd:.4f} vs ${result_a.cost_usd:.4f})"

    # 3. Speed
    if result_a.duration_seconds < result_b.duration_seconds * 0.95:
        return "a", (
            f"{result_a.model} was faster ({result_a.duration_seconds:.1f}s vs {result_b.duration_seconds:.1f}s)"
        )
    if result_b.duration_seconds < result_a.duration_seconds * 0.95:
        return "b", (
            f"{result_b.model} was faster ({result_b.duration_seconds:.1f}s vs {result_a.duration_seconds:.1f}s)"
        )

    return "tie", "results are within tolerance on cost and speed"


# ---------------------------------------------------------------------------
# Task creation & polling
# ---------------------------------------------------------------------------


def _create_task(
    client: httpx.Client,
    server_url: str,
    config: ABTestConfig,
    test_id: str,
    variant: Literal["a", "b"],
    model: str,
) -> str:
    """Create a single task on the server and return its ID.

    Args:
        client: httpx Client for HTTP calls.
        server_url: Base URL of the task server.
        config: A/B test configuration.
        test_id: Unique test run identifier.
        variant: Which side of the A/B test.
        model: Model name to pin.

    Returns:
        The task ID assigned by the server.

    Raises:
        RuntimeError: If the server rejects the task.
    """
    payload: dict[str, Any] = {
        "title": f"[AB-{test_id[:8]}:{variant}] {config.task_description[:80]}",
        "description": config.task_description,
        "role": config.role,
        "scope": config.scope,
        "model": model,
        "metadata": {
            "ab_test_id": test_id,
            "ab_variant": variant,
        },
    }
    resp = client.post(f"{server_url}/tasks", json=payload, timeout=10.0)
    resp.raise_for_status()
    data: dict[str, Any] = resp.json()
    task_id = data.get("id")
    if not task_id:
        msg = f"Server did not return a task ID: {data}"
        raise RuntimeError(msg)
    return str(task_id)


_TERMINAL_STATUSES = frozenset({"done", "failed", "cancelled", "closed"})


def _poll_task(
    client: httpx.Client,
    server_url: str,
    task_id: str,
    timeout_seconds: int,
) -> dict[str, Any]:
    """Poll until task reaches a terminal status or timeout.

    Args:
        client: httpx Client for HTTP calls.
        server_url: Base URL of the task server.
        task_id: Task to poll.
        timeout_seconds: Max seconds to wait.

    Returns:
        The final task dict from the server.

    Raises:
        TimeoutError: If the task does not finish in time.
    """
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        resp = client.get(f"{server_url}/tasks/{task_id}", timeout=10.0)
        resp.raise_for_status()
        task_data: dict[str, Any] = resp.json()
        status = str(task_data.get("status", ""))
        if status in _TERMINAL_STATUSES:
            return task_data
        time.sleep(_POLL_INTERVAL_SECONDS)
    raise TimeoutError(f"Task {task_id} did not complete within {timeout_seconds}s")


def _extract_result(
    task_data: dict[str, Any],
    model: str,
    variant: Literal["a", "b"],
    task_id: str,
    duration: float,
) -> ABTestResult:
    """Build an ABTestResult from a completed task dict.

    Args:
        task_data: Raw task dict from the server.
        model: Model name used.
        variant: A/B variant label.
        task_id: Task identifier.
        duration: Wall-clock duration in seconds.

    Returns:
        Populated ABTestResult.
    """
    status = str(task_data.get("status", "unknown"))
    passed = status in ("done", "closed")

    # Token / cost data may live in metadata or top-level fields
    meta: dict[str, Any] = task_data.get("metadata", {})
    cost = float(meta.get("cost_usd", task_data.get("cost_usd", 0.0)))
    input_tokens = int(meta.get("input_tokens", task_data.get("input_tokens", 0)))
    output_tokens = int(meta.get("output_tokens", task_data.get("output_tokens", 0)))
    quality_passed = bool(meta.get("quality_passed", task_data.get("quality_passed", passed)))

    return ABTestResult(
        model=model,
        variant=variant,
        task_id=task_id,
        duration_seconds=duration,
        cost_usd=cost,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        passed=passed,
        quality_passed=quality_passed,
        status=status,
    )


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def run_ab_test(config: ABTestConfig, server_url: str) -> ABTestReport:
    """Execute an A/B test: create two tasks, wait, compare.

    Args:
        config: What to test and which models to compare.
        server_url: Base URL of the running task server.

    Returns:
        An ABTestReport with metrics and winner determination.
    """
    test_id = uuid.uuid4().hex
    timed_out = False

    with httpx.Client() as client:
        # Create both tasks
        task_id_a = _create_task(client, server_url, config, test_id, "a", config.model_a)
        task_id_b = _create_task(client, server_url, config, test_id, "b", config.model_b)

        logger.info(
            "A/B test %s: task_a=%s (%s) task_b=%s (%s)",
            test_id[:8],
            task_id_a,
            config.model_a,
            task_id_b,
            config.model_b,
        )

        start = time.monotonic()

        # Poll both (sequentially — good enough for two tasks)
        try:
            data_a = _poll_task(client, server_url, task_id_a, config.timeout_seconds)
        except TimeoutError:
            timed_out = True
            data_a = {"status": "timeout"}

        remaining = max(1, config.timeout_seconds - int(time.monotonic() - start))
        try:
            data_b = _poll_task(client, server_url, task_id_b, remaining)
        except TimeoutError:
            timed_out = True
            data_b = {"status": "timeout"}

        end = time.monotonic()

    elapsed_a = float(data_a.get("duration_seconds", end - start))
    elapsed_b = float(data_b.get("duration_seconds", end - start))

    result_a = _extract_result(data_a, config.model_a, "a", task_id_a, elapsed_a)
    result_b = _extract_result(data_b, config.model_b, "b", task_id_b, elapsed_b)

    winner, reason = determine_winner(result_a, result_b)

    return ABTestReport(
        test_id=test_id,
        config=config,
        result_a=result_a,
        result_b=result_b,
        winner=winner,
        reason=reason,
        timed_out=timed_out,
    )
