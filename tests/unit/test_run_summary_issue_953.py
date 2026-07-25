"""Regression tests for issue gh-953.

Three layers from the bug report are covered:

1. **Producer/consumer shape mismatch** - the task server's ``/status``
   endpoint emits ``agents`` as ``{"count": N, "items": [...]}`` (matching
   the ``tasks`` section), but the run-summary consumer used to iterate it
   as a flat ``list[dict]``, which yielded the dict's string keys
   (``"count"``, ``"items"``) and crashed on ``str.get``.  We assert the
   normalization helper extracts ``items`` correctly and that the ``items``
   list always contains dicts (never bare strings) for partial/cancelled
   agents.

2. **Defensive parser** - covered by ``test_cli_ui.py`` (bare-string id is
   accepted).  Re-asserted here at the call-site level via
   ``render_run_summary_from_dict``.

3. **Cleanup ordering** - ``_finalize_run_output`` must drain
   ``.sdd/backlog/claimed/`` even when the summary renderer raises.
"""

# pyright: reportPrivateUsage=false

from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from bernstein.cli.run import _normalize_agent_entries, render_run_summary_from_dict
from bernstein.cli.ui import AgentInfo
from bernstein.core.routes.status_dashboard import _status_agent_items

# ---------------------------------------------------------------------------
# Layer 1: producer/consumer shape
# ---------------------------------------------------------------------------


class TestNormalizeAgentEntries:
    """The consumer-side normalizer must accept either shape."""

    def test_section_dict_shape_returns_items(self) -> None:
        """``/status`` emits ``{"count": N, "items": [...]}``."""
        payload: dict[str, Any] = {
            "count": 2,
            "items": [
                {"id": "a1", "role": "backend"},
                {"id": "a2", "role": "qa"},
            ],
        }
        out = _normalize_agent_entries(payload)
        assert len(out) == 2
        assert out[0]["id"] == "a1"
        assert out[1]["id"] == "a2"

    def test_legacy_list_shape_passthrough(self) -> None:
        """Older callers still pass a flat list - keep working."""
        payload: list[dict[str, Any]] = [{"id": "a1"}, {"id": "a2"}]
        out = _normalize_agent_entries(payload)
        assert [a["id"] for a in out] == ["a1", "a2"]

    def test_drops_non_dict_items(self) -> None:
        """Bare strings inside ``items`` are dropped (not crashed on)."""
        payload: dict[str, Any] = {
            "count": 2,
            "items": [{"id": "a1"}, "stray-id-string", 42],
        }
        out = _normalize_agent_entries(payload)
        assert len(out) == 1
        assert out[0]["id"] == "a1"

    def test_empty_or_unexpected_payload_returns_empty(self) -> None:
        assert _normalize_agent_entries(None) == []
        assert _normalize_agent_entries("garbage") == []
        assert _normalize_agent_entries({}) == []
        assert _normalize_agent_entries({"items": "not-a-list"}) == []


class TestStatusAgentItemsProducer:
    """Layer 1 (producer): ``_status_agent_items`` must always emit dicts.

    The reporter hinted that "partial/cancelled agents serialize to just
    their ID".  We assert here that the producer always returns full dicts
    even when fed a snapshot-only path with no live agents.
    """

    def test_emits_dicts_from_snapshots_only(self) -> None:
        store = MagicMock()
        store.agents = {}
        snapshots = {
            "agent-x": {
                "id": "agent-x",
                "role": "backend",
                "status": "dead",
            }
        }
        items = _status_agent_items(store, snapshots, {}, now=1000.0)
        assert len(items) == 1
        assert isinstance(items[0], dict)
        assert items[0]["id"] == "agent-x"
        # Must not be a bare string anywhere.
        assert all(isinstance(item, dict) for item in items)


# ---------------------------------------------------------------------------
# Layer 2: end-to-end via the renderer
# ---------------------------------------------------------------------------


class TestRenderRunSummaryFromDict:
    """The renderer must survive both shapes without raising."""

    def test_section_dict_shape_does_not_crash(self) -> None:
        """Reproduction of gh-953: ``agents`` is a section dict.

        Pre-fix this raised ``AttributeError: 'str' object has no attribute
        'get'`` because the comprehension iterated dict keys.
        """
        data: dict[str, Any] = {
            "summary": {"total": 1, "open": 0, "claimed": 0, "done": 1, "failed": 0},
            "agents": {
                "count": 1,
                "items": [
                    {
                        "id": "backend-d3c7c7bb",
                        "role": "backend",
                        "model": "sonnet",
                        "status": "done",
                        "tokens_used": 12_345,
                    }
                ],
            },
            "elapsed_seconds": 30.4,
            "total_cost_usd": 0.0123,
        }
        # Should not raise.
        render_run_summary_from_dict(data, console=MagicMock())

    def test_legacy_list_shape_still_works(self) -> None:
        data: dict[str, Any] = {
            "summary": {"total": 0},
            "agents": [{"id": "a1", "role": "qa"}],
        }
        render_run_summary_from_dict(data, console=MagicMock())

    def test_bare_string_inside_items_does_not_crash(self) -> None:
        """Belt-and-braces: even if a bare string sneaks past, no crash."""
        data: dict[str, Any] = {
            "summary": {"total": 0},
            "agents": {"count": 1, "items": ["stray-id"]},
        }
        # ``_normalize_agent_entries`` drops non-dicts; renderer is happy.
        render_run_summary_from_dict(data, console=MagicMock())


# ---------------------------------------------------------------------------
# Layer 3: cleanup ordering
# ---------------------------------------------------------------------------


class TestCleanupOrdering:
    """``_finalize_run_output`` must drain ``claimed/`` even on render crash."""

    def test_drain_runs_after_successful_summary(self) -> None:
        from bernstein.cli.run_preflight import _finalize_run_output

        with (
            patch("bernstein.cli.run_bootstrap._wait_for_run_completion"),
            patch("bernstein.cli.run_preflight._show_run_summary") as show_summary,
            patch("bernstein.cli.run_preflight._drain_completed_backlog_files") as drain,
        ):
            _finalize_run_output(quiet=True)

        show_summary.assert_called_once()
        drain.assert_called_once()

    def test_drain_runs_when_renderer_raises(self) -> None:
        """gh-953 layer 3: a UI crash must NOT leak claimed tickets."""
        from bernstein.cli.run_preflight import _finalize_run_output

        with (
            patch("bernstein.cli.run_bootstrap._wait_for_run_completion"),
            patch(
                "bernstein.cli.run_preflight._show_run_summary",
                side_effect=AttributeError("'str' object has no attribute 'get'"),
            ) as show_summary,
            patch("bernstein.cli.run_preflight._drain_completed_backlog_files") as drain,
        ):
            with pytest.raises(AttributeError):
                _finalize_run_output(quiet=True)

        show_summary.assert_called_once()
        # The whole point: cleanup ran even though rendering exploded.
        drain.assert_called_once()

    def test_quiet_run_exits_nonzero_when_task_never_completed(self) -> None:
        """Issue #3010: the synchronous (quiet) completion path must exit
        non-zero when QUIESCENCE WAS DETECTED and the run still ended with a
        declared task neither done nor failed -- an operator scripting
        ``bernstein run && deploy`` must not deploy on a run that produced
        nothing.

        ``_wait_for_run_completion`` returns a payload only when quiescence was
        actually observed, so a returned payload here means "the run really
        ended in this state", not "it was still working". The still-in-flight
        case is covered by the timeout test below.
        """
        from bernstein.core.retrospective import EXIT_RUN_UNHEALTHY

        from bernstein.cli.run_preflight import _finalize_run_output

        ended_stuck_status = {"total": 1, "done": 0, "failed": 0, "open": 1}
        with (
            patch("bernstein.cli.run_bootstrap._wait_for_run_completion", return_value=ended_stuck_status),
            patch("bernstein.cli.run_preflight._show_run_summary"),
            patch("bernstein.cli.run_preflight._drain_completed_backlog_files") as drain,
        ):
            with pytest.raises(SystemExit) as exc_info:
                _finalize_run_output(quiet=True)
        assert exc_info.value.code == EXIT_RUN_UNHEALTHY
        assert exc_info.value.code != 0
        # Cleanup still runs on the non-zero-exit path.
        drain.assert_called_once()

    def test_quiet_run_exits_nonzero_on_quiescent_run_with_failed_task(self) -> None:
        """The exit mapping fires on any observable bad outcome, not just the
        never-terminated case: a quiescent run with a failed task exits
        non-zero."""
        from bernstein.core.retrospective import EXIT_RUN_UNHEALTHY

        from bernstein.cli.run_preflight import _finalize_run_output

        failed_status = {"total": 2, "done": 1, "failed": 1, "open": 0}
        with (
            patch("bernstein.cli.run_bootstrap._wait_for_run_completion", return_value=failed_status),
            patch("bernstein.cli.run_preflight._show_run_summary"),
            patch("bernstein.cli.run_preflight._drain_completed_backlog_files"),
        ):
            with pytest.raises(SystemExit) as exc_info:
                _finalize_run_output(quiet=True)
        assert exc_info.value.code == EXIT_RUN_UNHEALTHY

    def test_quiet_run_exits_zero_when_wait_times_out_still_in_flight(self) -> None:
        """A healthy run that simply outlives the CLI wait deadline must exit 0.

        ``_wait_for_run_completion`` returns None when quiescence was never
        observed (the run is still working in the background). That is a
        "no verdict" case, NOT a failure: multi-hour goals are designed for
        (scope timeouts reach 7200s against a 3600s default wait), so treating
        the timeout as unhealthy would break ``bernstein run && deploy`` in the
        opposite direction from the bug this PR fixes.
        """
        from bernstein.cli.run_preflight import _finalize_run_output

        with (
            patch("bernstein.cli.run_bootstrap._wait_for_run_completion", return_value=None),
            patch("bernstein.cli.run_preflight._show_run_summary"),
            patch("bernstein.cli.run_preflight._drain_completed_backlog_files") as drain,
        ):
            # Must return normally (exit 0), not raise SystemExit.
            _finalize_run_output(quiet=True)
        drain.assert_called_once()

    def test_issue_3010_end_to_end_stuck_task_orchestrator_gone_exits_nonzero(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Issue #3010 end-to-end, through the REAL wait + REAL pidfile check.

        The exact reported shape: one declared task that never completed, its
        agent produced nothing, and the orchestrator has exited and stayed gone
        for longer than a recovery restart would have taken. Only the server
        polls and the clock are faked -- ``_wait_for_run_completion``, the
        orchestrator-liveness classification and the confirmation window are
        the real ones, reading a real stale ``spawner.pid``.
        ``bernstein run --quiet && deploy`` must not deploy on this run.
        """
        from bernstein.core.retrospective import EXIT_RUN_UNHEALTHY

        import bernstein.cli.run_bootstrap as rb
        from bernstein.cli.run_preflight import _finalize_run_output

        runtime = tmp_path / ".sdd" / "runtime"
        runtime.mkdir(parents=True)
        # Orchestrator ran and exited: pidfile present, pid not alive.
        (runtime / "spawner.pid").write_text("999999")
        monkeypatch.chdir(tmp_path)

        stuck = {"total": 1, "open": 1, "claimed": 0, "done": 0, "failed": 0}
        counts = {"total": 1, "open": 1, "claimed": 0, "in_progress": 0, "orphaned": 0, "done": 0, "failed": 0}

        # Monotonic and wall advance together, as a real time.sleep does: the
        # confirmation window is measured on monotonic.
        elapsed = {"s": 0.0}
        base_wall, base_mono = time.time(), time.monotonic()
        clock = SimpleNamespace(
            time=lambda: base_wall + elapsed["s"],
            monotonic=lambda: base_mono + elapsed["s"],
            sleep=lambda _s: elapsed.__setitem__("s", elapsed["s"] + 2.0),
        )

        def _fake_get(path: str) -> object:
            if path == "/status":
                return stuck
            if path == "/tasks/counts":
                return counts
            return {"agent_count": 0}

        with (
            patch.object(rb, "server_get", side_effect=_fake_get),
            patch.object(rb, "time", clock),
            patch.object(rb, "_signal_orchestrator_shutdown"),
            patch("bernstein.cli.run_preflight._show_run_summary"),
            patch("bernstein.cli.run_preflight._drain_completed_backlog_files"),
        ):
            with pytest.raises(SystemExit) as exc_info:
                _finalize_run_output(quiet=True)

        assert exc_info.value.code == EXIT_RUN_UNHEALTHY
        assert exc_info.value.code != 0

    def test_startup_window_with_live_orchestrator_does_not_exit_nonzero(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Counterpart guard: identical task counts, but the orchestrator is
        ALIVE (startup window) -- must reach the deadline and exit 0, never be
        mistaken for a finished run."""
        import os

        import bernstein.cli.run_bootstrap as rb
        from bernstein.cli.run_preflight import _finalize_run_output

        runtime = tmp_path / ".sdd" / "runtime"
        runtime.mkdir(parents=True)
        # A genuinely live pid: this test process.
        (runtime / "spawner.pid").write_text(str(os.getpid()))
        monkeypatch.chdir(tmp_path)

        starting = {"total": 1, "open": 1, "claimed": 0, "done": 0, "failed": 0}
        clock = {"t": time.time()}

        def _fake_time() -> float:
            clock["t"] += 10.0
            return clock["t"]

        with (
            patch.object(rb, "server_get", side_effect=lambda p: starting if p == "/status" else {"agent_count": 0}),
            patch.object(rb.time, "sleep", return_value=None),
            patch.object(rb.time, "time", side_effect=_fake_time),
            patch.object(rb, "_signal_orchestrator_shutdown"),
            patch("bernstein.cli.run_preflight._show_run_summary"),
            patch("bernstein.cli.run_preflight._drain_completed_backlog_files"),
        ):
            # Returns normally -> exit 0.
            _finalize_run_output(quiet=True)

    def test_quiet_run_exits_zero_when_all_done(self) -> None:
        from bernstein.cli.run_preflight import _finalize_run_output

        done_status = {"total": 2, "done": 2, "failed": 0, "open": 0}
        with (
            patch("bernstein.cli.run_bootstrap._wait_for_run_completion", return_value=done_status),
            patch("bernstein.cli.run_preflight._show_run_summary"),
            patch("bernstein.cli.run_preflight._drain_completed_backlog_files"),
        ):
            # Returns normally (no SystemExit) -> exit code 0.
            _finalize_run_output(quiet=True)

    def test_drain_is_a_noop_when_no_claimed_dir(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """When ``.sdd/backlog/claimed/`` doesn't exist, drain returns silently."""
        from bernstein.cli.run_preflight import _drain_completed_backlog_files

        monkeypatch.chdir(tmp_path)
        # No .sdd/ at all - must not raise, must not call sync internals.
        with patch("bernstein.core.sync._move_completed_files") as move_files:
            _drain_completed_backlog_files()
        move_files.assert_not_called()

    def test_drain_invokes_move_completed_files(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """When ``claimed/`` exists, drain calls the sync mover."""
        from bernstein.cli.run_preflight import _drain_completed_backlog_files

        claimed = tmp_path / ".sdd" / "backlog" / "claimed"
        claimed.mkdir(parents=True)
        monkeypatch.chdir(tmp_path)
        with patch("bernstein.core.sync._move_completed_files") as move_files:
            _drain_completed_backlog_files()
        move_files.assert_called_once()

    def test_drain_swallows_internal_exceptions(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """A failure inside the mover must not propagate (cleanup is best-effort)."""
        from bernstein.cli.run_preflight import _drain_completed_backlog_files

        claimed = tmp_path / ".sdd" / "backlog" / "claimed"
        claimed.mkdir(parents=True)
        monkeypatch.chdir(tmp_path)
        with patch(
            "bernstein.core.sync._move_completed_files",
            side_effect=RuntimeError("server gone"),
        ):
            # Must not raise.
            _drain_completed_backlog_files()


# ---------------------------------------------------------------------------
# Type sanity
# ---------------------------------------------------------------------------


def test_agent_info_from_dict_str_or_dict_typing() -> None:
    """Layer 2 sanity: both shapes return AgentInfo with consistent type."""
    a = AgentInfo.from_dict({"id": "a1", "role": "backend"})
    b = AgentInfo.from_dict("a1")
    assert isinstance(a, AgentInfo)
    assert isinstance(b, AgentInfo)
    assert a.agent_id == b.agent_id == "a1"
