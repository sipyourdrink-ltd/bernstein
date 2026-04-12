"""Tests for reactive 413 context-overflow compaction fallback handler.

Covers:
- Log pattern detection for 413 / context-overflow errors
- detect_failure_type returning "context_overflow"
- _try_compact_and_retry executing the compaction pipeline and retrying
- Retry limit enforcement (max 1 compaction retry)
- handle_orphaned_task integration with context_overflow path
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from bernstein.core.agent_lifecycle import (
    _COMPACT_MAX_RETRIES,
    _COMPACT_RETRY_META,
    _try_compact_and_retry,
    handle_orphaned_task,
)
from bernstein.core.models import (
    AgentSession,
    Complexity,
    ModelConfig,
    Scope,
    Task,
    TaskStatus,
    TaskType,
)
from bernstein.core.rate_limit_tracker import (
    _CONTEXT_OVERFLOW_PATTERNS,
    RateLimitTracker,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_task(task_id: str = "T-413", meta_messages: list[str] | None = None) -> Task:
    return Task(
        id=task_id,
        title="Implement feature",
        description="Write the code for the new feature module.\n" * 20,
        role="backend",
        status=TaskStatus.OPEN,
        scope=Scope.MEDIUM,
        complexity=Complexity.MEDIUM,
        task_type=TaskType.STANDARD,
        meta_messages=meta_messages or [],
    )


def _make_session(task_id: str = "T-413") -> AgentSession:
    return AgentSession(
        id="sess-413",
        role="backend",
        provider="claude",
        model_config=ModelConfig("sonnet", "high"),
        task_ids=[task_id],
    )


def _ok_response() -> MagicMock:
    response = MagicMock()
    response.raise_for_status.return_value = None
    response.json.return_value = []
    return response


def _make_orch(tmp_path: Path, failure_type: str = "context_overflow") -> SimpleNamespace:
    tracker = MagicMock()
    tracker.detect_failure_type.return_value = failure_type
    tracker.throttle_summary.return_value = {"claude": {"until": 999}}
    tracker.is_throttled.side_effect = lambda provider: provider == "claude"

    orch = SimpleNamespace()
    orch._config = SimpleNamespace(
        server_url="http://server",
        max_task_retries=3,
        recovery="restart",
        max_crash_retries=3,
    )
    orch._client = MagicMock()
    orch._client.patch.return_value = _ok_response()
    orch._client.post.return_value = _ok_response()
    orch._client.get.return_value = _ok_response()
    orch._workdir = tmp_path
    orch._rate_limit_tracker = tracker
    orch._router = None
    orch._cascade_manager = MagicMock()
    orch._cascade_manager.find_fallback.return_value = MagicMock(
        fallback_provider="codex",
        fallback_model="gpt-5.4-mini",
        reason="context overflow",
    )
    orch._retried_task_ids: set[str] = set()
    orch._record_provider_health = MagicMock()
    orch._evolution = None
    orch._wal_writer = None
    orch._crash_counts: dict[str, int] = {}
    orch._spawner = MagicMock()
    orch._spawner.get_worktree_path.return_value = None
    orch._plugin_manager = None
    return orch


def _snapshot(task: Task) -> dict[str, list[Task]]:
    return {"open": [task], "claimed": [], "in_progress": [], "done": []}


# ---------------------------------------------------------------------------
# RateLimitTracker: 413 / context-overflow detection
# ---------------------------------------------------------------------------


class TestContextOverflowPatterns:
    """Verify that _CONTEXT_OVERFLOW_PATTERNS are defined and correct."""

    def test_patterns_tuple_is_not_empty(self) -> None:
        assert len(_CONTEXT_OVERFLOW_PATTERNS) > 0

    @pytest.mark.parametrize(
        "pattern",
        [
            "413",
            "prompt is too long",
            "prompt_too_long",
            "context_length_exceeded",
            "maximum context length",
            "request too large",
        ],
    )
    def test_expected_patterns_present(self, pattern: str) -> None:
        assert pattern in _CONTEXT_OVERFLOW_PATTERNS


class TestScanLogForContextOverflow:
    """Test scan_log_for_context_overflow on various log snippets."""

    def test_detects_http_413(self, tmp_path: Path) -> None:
        log = tmp_path / "agent.log"
        log.write_text("INFO: Starting task\nERROR: HTTP 413 Request Entity Too Large\n")
        tracker = RateLimitTracker()
        assert tracker.scan_log_for_context_overflow(log) is True

    def test_detects_prompt_too_long(self, tmp_path: Path) -> None:
        log = tmp_path / "agent.log"
        log.write_text("ERROR: prompt is too long for model context window\n")
        tracker = RateLimitTracker()
        assert tracker.scan_log_for_context_overflow(log) is True

    def test_detects_context_length_exceeded(self, tmp_path: Path) -> None:
        log = tmp_path / "agent.log"
        log.write_text('{"error": {"type": "context_length_exceeded", "message": "too many tokens"}}\n')
        tracker = RateLimitTracker()
        assert tracker.scan_log_for_context_overflow(log) is True

    def test_detects_maximum_context_length(self, tmp_path: Path) -> None:
        log = tmp_path / "agent.log"
        log.write_text("Error: This request exceeds the maximum context length of 200000 tokens.\n")
        tracker = RateLimitTracker()
        assert tracker.scan_log_for_context_overflow(log) is True

    def test_returns_false_for_clean_log(self, tmp_path: Path) -> None:
        log = tmp_path / "agent.log"
        log.write_text("INFO: Task completed successfully.\nDone.\n")
        tracker = RateLimitTracker()
        assert tracker.scan_log_for_context_overflow(log) is False

    def test_returns_false_for_missing_file(self, tmp_path: Path) -> None:
        tracker = RateLimitTracker()
        assert tracker.scan_log_for_context_overflow(tmp_path / "nonexistent.log") is False


class TestDetectFailureTypeContextOverflow:
    """detect_failure_type returns 'context_overflow' for 413 errors."""

    def test_returns_context_overflow_for_413_log(self, tmp_path: Path) -> None:
        log = tmp_path / "agent.log"
        log.write_text("ERROR: 413 Payload Too Large — prompt is too long\n")
        tracker = RateLimitTracker()
        assert tracker.detect_failure_type(log) == "context_overflow"

    def test_rate_limit_takes_precedence_over_context_overflow(self, tmp_path: Path) -> None:
        """If both 429 and 413 patterns appear, rate_limit wins (checked first)."""
        log = tmp_path / "agent.log"
        log.write_text("ERROR: 429 Too Many Requests\nERROR: prompt is too long\n")
        tracker = RateLimitTracker()
        assert tracker.detect_failure_type(log) == "rate_limit"


# ---------------------------------------------------------------------------
# _try_compact_and_retry
# ---------------------------------------------------------------------------


class TestTryCompactAndRetry:
    """Test the reactive compaction + retry flow."""

    def test_compacts_and_retries_successfully(self, tmp_path: Path) -> None:
        task = _make_task()
        session = _make_session()
        orch = _make_orch(tmp_path)

        # Make the GET /tasks return a retry task
        retry_task_resp = MagicMock()
        retry_task_resp.raise_for_status.return_value = None
        retry_task_resp.json.return_value = [
            {"id": "T-retry-1", "title": "[RETRY 1] Implement feature", "status": "open"},
        ]
        orch._client.get.return_value = retry_task_resp

        with patch("bernstein.core.agents.agent_state_refresh.retry_or_fail_task") as mock_retry:
            result = _try_compact_and_retry(
                orch=orch,
                task=task,
                task_id=task.id,
                session=session,
                tasks_snapshot=_snapshot(task),
                fallback_model=None,
            )

        assert result is True
        mock_retry.assert_called_once()
        # Verify retry was called with a 413-specific reason
        call_kwargs = mock_retry.call_args
        assert "413" in call_kwargs[0][1] or "Context overflow" in call_kwargs[0][1]

    def test_fails_permanently_when_compact_retries_exhausted(self, tmp_path: Path) -> None:
        task = _make_task(meta_messages=["CONTEXT COMPACTION: already compacted once"])
        session = _make_session()
        orch = _make_orch(tmp_path)

        with patch("bernstein.core.agents.agent_state_refresh.retry_or_fail_task") as mock_retry:
            result = _try_compact_and_retry(
                orch=orch,
                task=task,
                task_id=task.id,
                session=session,
                tasks_snapshot=_snapshot(task),
                fallback_model=None,
            )

        assert result is False
        # retry_or_fail_task should be called with max_task_retries=0 to force fail
        mock_retry.assert_called_once()
        assert mock_retry.call_args[1]["max_task_retries"] == 0

    def test_falls_back_on_pipeline_exception(self, tmp_path: Path) -> None:
        task = _make_task()
        session = _make_session()
        orch = _make_orch(tmp_path)

        with (
            patch(
                "bernstein.core.compaction_pipeline.CompactionPipeline.execute",
                side_effect=RuntimeError("pipeline boom"),
            ),
            patch("bernstein.core.agents.agent_state_refresh.retry_or_fail_task") as mock_retry,
        ):
            result = _try_compact_and_retry(
                orch=orch,
                task=task,
                task_id=task.id,
                session=session,
                tasks_snapshot=_snapshot(task),
                fallback_model=None,
            )

        assert result is False
        mock_retry.assert_called_once()
        assert "compaction failed" in mock_retry.call_args[0][1].lower()

    def test_writes_wal_entry_on_success(self, tmp_path: Path) -> None:
        task = _make_task()
        session = _make_session()
        orch = _make_orch(tmp_path)
        orch._wal_writer = MagicMock()

        retry_task_resp = MagicMock()
        retry_task_resp.raise_for_status.return_value = None
        retry_task_resp.json.return_value = []
        orch._client.get.return_value = retry_task_resp

        with patch("bernstein.core.agents.agent_state_refresh.retry_or_fail_task"):
            _try_compact_and_retry(
                orch=orch,
                task=task,
                task_id=task.id,
                session=session,
                tasks_snapshot=_snapshot(task),
                fallback_model=None,
            )

        orch._wal_writer.write_entry.assert_called_once()
        wal_call = orch._wal_writer.write_entry.call_args
        assert wal_call[1]["decision_type"] == "context_overflow_compacted"
        assert wal_call[1]["output"]["compacted"] is True

    def test_patches_retry_task_with_compacted_description(self, tmp_path: Path) -> None:
        task = _make_task()
        session = _make_session()
        orch = _make_orch(tmp_path)

        # Simulate finding the retry task in open tasks list
        retry_task_resp = MagicMock()
        retry_task_resp.raise_for_status.return_value = None
        retry_task_resp.json.return_value = [
            {"id": "T-retry-1", "title": "[RETRY 1] Implement feature", "status": "open"},
        ]
        orch._client.get.return_value = retry_task_resp

        with patch("bernstein.core.agents.agent_state_refresh.retry_or_fail_task"):
            _try_compact_and_retry(
                orch=orch,
                task=task,
                task_id=task.id,
                session=session,
                tasks_snapshot=_snapshot(task),
                fallback_model="gpt-5.4-mini",
            )

        # The PATCH call should include compacted description and meta_messages
        patch_calls = [c for c in orch._client.patch.call_args_list if "T-retry-1" in str(c)]
        assert len(patch_calls) == 1
        patch_body = patch_calls[0][1]["json"]
        assert "description" in patch_body
        assert _COMPACT_RETRY_META in patch_body["meta_messages"]
        assert patch_body["model"] == "gpt-5.4-mini"


# ---------------------------------------------------------------------------
# handle_orphaned_task integration: context_overflow branch
# ---------------------------------------------------------------------------


class TestHandleOrphanedTaskContextOverflow:
    """handle_orphaned_task triggers compaction on context_overflow."""

    def test_context_overflow_triggers_compact_and_retry(self, tmp_path: Path) -> None:
        task = _make_task()
        session = _make_session()
        orch = _make_orch(tmp_path, failure_type="context_overflow")

        # Create log file that triggers context_overflow detection
        sdd_runtime = tmp_path / ".sdd" / "runtime"
        sdd_runtime.mkdir(parents=True)
        log_file = sdd_runtime / f"{session.id}.log"
        log_file.write_text("ERROR: 413 prompt is too long\n")

        with (
            patch("bernstein.core.agents.agent_state_refresh._try_compact_and_retry", return_value=True) as mock_compact,
            patch("bernstein.core.agents.agent_reaping.emit_orphan_metrics"),
        ):
            handle_orphaned_task(orch, task.id, session, _snapshot(task))

        mock_compact.assert_called_once()
        orch._record_provider_health.assert_called_once_with(session, success=False)

    def test_context_overflow_compact_failure_still_emits_metrics(self, tmp_path: Path) -> None:
        task = _make_task()
        session = _make_session()
        orch = _make_orch(tmp_path, failure_type="context_overflow")

        sdd_runtime = tmp_path / ".sdd" / "runtime"
        sdd_runtime.mkdir(parents=True)
        (sdd_runtime / f"{session.id}.log").write_text("ERROR: 413\n")

        with (
            patch("bernstein.core.agents.agent_state_refresh._try_compact_and_retry", return_value=False),
            patch("bernstein.core.agents.agent_reaping.emit_orphan_metrics") as mock_metrics,
        ):
            handle_orphaned_task(orch, task.id, session, _snapshot(task))

        mock_metrics.assert_called_once()
        metrics_kwargs = mock_metrics.call_args[1]
        assert metrics_kwargs["error_type"] == "context_overflow_compact_failed"


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


class TestConstants:
    def test_compact_max_retries_is_one(self) -> None:
        assert _COMPACT_MAX_RETRIES == 1

    def test_compact_retry_meta_mentions_413(self) -> None:
        assert "413" in _COMPACT_RETRY_META

    def test_compact_retry_meta_mentions_compaction(self) -> None:
        assert "CONTEXT COMPACTION" in _COMPACT_RETRY_META
