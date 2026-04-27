"""Regression tests for issue gh-953.

Three layers from the bug report are covered:

1. **Producer/consumer shape mismatch** — the task server's ``/status``
   endpoint emits ``agents`` as ``{"count": N, "items": [...]}`` (matching
   the ``tasks`` section), but the run-summary consumer used to iterate it
   as a flat ``list[dict]``, which yielded the dict's string keys
   (``"count"``, ``"items"``) and crashed on ``str.get``.  We assert the
   normalization helper extracts ``items`` correctly and that the ``items``
   list always contains dicts (never bare strings) for partial/cancelled
   agents.

2. **Defensive parser** — covered by ``test_cli_ui.py`` (bare-string id is
   accepted).  Re-asserted here at the call-site level via
   ``render_run_summary_from_dict``.

3. **Cleanup ordering** — ``_finalize_run_output`` must drain
   ``.sdd/backlog/claimed/`` even when the summary renderer raises.
"""

# pyright: reportPrivateUsage=false

from __future__ import annotations

from pathlib import Path
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
        """Older callers still pass a flat list — keep working."""
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

    def test_drain_is_a_noop_when_no_claimed_dir(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """When ``.sdd/backlog/claimed/`` doesn't exist, drain returns silently."""
        from bernstein.cli.run_preflight import _drain_completed_backlog_files

        monkeypatch.chdir(tmp_path)
        # No .sdd/ at all — must not raise, must not call sync internals.
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
