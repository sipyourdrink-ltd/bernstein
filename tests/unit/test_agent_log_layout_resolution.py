"""Agent-log consumers must resolve the log across every worktree layout.

Two consumers of the per-agent log hardcoded only the legacy worktree
layout, ``.sdd/worktrees/<id>/.sdd/{runtime,logs}/<id>.log``, so they went
blind under the current default worktree layout
(``.sdd/runtime/worktrees/<id>/...``) and for any session that reports its
own ``session.log_path`` (the remote runtime bridge, container, and
sandbox-session spawn paths, which all log to
``<spawn_cwd>/.sdd/logs/<id>.log``):

* ``capture_cli_adapter_usage`` -> ``bernstein.core.cost.cli_adapter_usage``
  reads token/usage counters out of the agent log. A miss means usage
  accounting silently under-reports for the affected session.
* ``AgentLogAggregator.parse_log`` feeds
  ``bernstein.core.agents.heartbeat.check_stalled_tasks``. A miss means the
  stall verdict is made on an empty log summary.

Both now resolve through
:func:`bernstein.core.agents.agent_lifecycle._resolve_agent_worktree_dir`,
the same helper the reap tick's liveness probe uses (issue #3215), instead
of reimplementing worktree-layout resolution.
"""

from __future__ import annotations

import json
from pathlib import Path

from bernstein.core.agents.agent_log_aggregator import AgentLogAggregator
from bernstein.core.cost.cli_adapter_usage import capture_cli_adapter_usage

# A minimal qwen ``--output-format stream-json`` result record carrying the
# authoritative cumulative token breakdown - enough for
# ``capture_cli_adapter_usage`` to recognise nonzero usage.
_QWEN_RESULT_LOG = json.dumps(
    {
        "type": "result",
        "subtype": "success",
        "usage": {"input_tokens": 512, "output_tokens": 64},
        "stats": {"models": {"qwen3-coder-plus": {"tokens": {"prompt": 512, "completion": 64}}}},
    }
)

# Log content the aggregator can categorise into a nonempty summary.
_AGGREGATOR_LOG = "SyntaxError: invalid syntax\n"


def _touch(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


class TestCliAdapterUsageLayouts:
    """``capture_cli_adapter_usage`` must find the log in every layout."""

    def test_current_default_worktree_layout_is_missed(self, tmp_path: Path) -> None:
        """FAIL-NOTE (reproduces #3216): the current default worktree layout.

        ``.sdd/runtime/worktrees/<id>/.sdd/runtime/<id>.log`` is where a
        worktree-isolated session's log actually lives today. The hardcoded
        probe only ever looked at the legacy ``.sdd/worktrees/<id>`` base, so
        this session's real usage is silently zeroed.
        """
        log_path = tmp_path / ".sdd" / "runtime" / "worktrees" / "s1" / ".sdd" / "runtime" / "s1.log"
        _touch(log_path, _QWEN_RESULT_LOG)

        tokens_in, tokens_out, _ = capture_cli_adapter_usage(tmp_path, "s1")

        assert (tokens_in, tokens_out) == (512, 64), "current default worktree layout must be resolved"

    def test_legacy_worktree_layout_still_works(self, tmp_path: Path) -> None:
        """The one layout the hardcoded probe already covered must keep working."""
        log_path = tmp_path / ".sdd" / "worktrees" / "s1" / ".sdd" / "runtime" / "s1.log"
        _touch(log_path, _QWEN_RESULT_LOG)

        tokens_in, tokens_out, _ = capture_cli_adapter_usage(tmp_path, "s1")

        assert (tokens_in, tokens_out) == (512, 64)

    def test_worktrees_disabled_root_layout_still_works(self, tmp_path: Path) -> None:
        """With worktrees disabled, the log sits at the workdir root."""
        log_path = tmp_path / ".sdd" / "runtime" / "s1.log"
        _touch(log_path, _QWEN_RESULT_LOG)

        tokens_in, tokens_out, _ = capture_cli_adapter_usage(tmp_path, "s1")

        assert (tokens_in, tokens_out) == (512, 64)

    def test_bridge_session_log_path_is_preferred_outside_workdir(self, tmp_path: Path) -> None:
        """A remote-bridge session's ``log_path`` must win even if it is
        nowhere any candidate layout would look (a spawn cwd outside
        ``workdir`` entirely, as the runtime bridge / container / sandbox
        spawn paths use).
        """
        bridge_log = tmp_path / "elsewhere" / "spawn-cwd" / ".sdd" / "logs" / "s1.log"
        _touch(bridge_log, _QWEN_RESULT_LOG)
        # Poison the standard root candidate so a pass could only mean the
        # explicit log_path was actually consulted.
        _touch(tmp_path / ".sdd" / "runtime" / "s1.log", "")

        tokens_in, tokens_out, _ = capture_cli_adapter_usage(tmp_path, "s1", bridge_log)

        assert (tokens_in, tokens_out) == (512, 64)


class TestAgentLogAggregatorLayouts:
    """``AgentLogAggregator.parse_log`` must find the log in every layout."""

    def test_current_default_worktree_layout_is_missed(self, tmp_path: Path) -> None:
        """FAIL-NOTE (reproduces #3216): current default worktree layout missed."""
        log_path = tmp_path / ".sdd" / "runtime" / "worktrees" / "agent-1" / ".sdd" / "runtime" / "agent-1.log"
        _touch(log_path, _AGGREGATOR_LOG)

        summary = AgentLogAggregator(tmp_path).parse_log("agent-1")

        assert summary.compile_errors == 1, "current default worktree layout must be resolved"

    def test_legacy_worktree_layout_still_works(self, tmp_path: Path) -> None:
        log_path = tmp_path / ".sdd" / "worktrees" / "agent-1" / ".sdd" / "runtime" / "agent-1.log"
        _touch(log_path, _AGGREGATOR_LOG)

        summary = AgentLogAggregator(tmp_path).parse_log("agent-1")

        assert summary.compile_errors == 1

    def test_worktrees_disabled_root_layout_still_works(self, tmp_path: Path) -> None:
        log_path = tmp_path / ".sdd" / "runtime" / "agent-1.log"
        _touch(log_path, _AGGREGATOR_LOG)

        summary = AgentLogAggregator(tmp_path).parse_log("agent-1")

        assert summary.compile_errors == 1

    def test_session_reported_log_path_is_missed(self, tmp_path: Path) -> None:
        """FAIL-NOTE (reproduces #3216): ``parse_log`` has no way to prefer
        a session-reported log path at all today - a bridge/container/
        sandbox session whose log lives outside every candidate layout gets
        an empty summary, which starves ``check_stalled_tasks`` of input.
        """
        bridge_log = tmp_path / "elsewhere" / "spawn-cwd" / ".sdd" / "logs" / "agent-1.log"
        _touch(bridge_log, _AGGREGATOR_LOG)

        summary = AgentLogAggregator(tmp_path).parse_log("agent-1", log_path=bridge_log)

        assert summary.compile_errors == 1, "an explicit session log_path must be preferred"
