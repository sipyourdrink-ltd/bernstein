"""Tests for approval gates — gating merge after janitor verification.

Tests follow TDD: written before implementation to define the API.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

from bernstein.core.models import (
    Complexity,
    OrchestratorConfig,
    Scope,
    Task,
    TaskStatus,
    TaskType,
)

if TYPE_CHECKING:
    from pathlib import Path


def _make_task(
    *,
    id: str = "T-001",
    title: str = "Add auth",
    role: str = "backend",
) -> Task:
    return Task(
        id=id,
        title=title,
        description="Implement auth.",
        role=role,
        priority=2,
        scope=Scope.MEDIUM,
        complexity=Complexity.MEDIUM,
        status=TaskStatus.DONE,
        task_type=TaskType.STANDARD,
    )


# ---------------------------------------------------------------------------
# OrchestratorConfig has approval field
# ---------------------------------------------------------------------------


class TestOrchestratorConfigApproval:
    def test_default_approval_is_auto(self) -> None:
        cfg = OrchestratorConfig()
        assert cfg.approval == "auto"

    def test_approval_can_be_set_to_review(self) -> None:
        cfg = OrchestratorConfig(approval="review")
        assert cfg.approval == "review"

    def test_approval_can_be_set_to_pr(self) -> None:
        cfg = OrchestratorConfig(approval="pr")
        assert cfg.approval == "pr"


# ---------------------------------------------------------------------------
# ApprovalGate — auto mode
# ---------------------------------------------------------------------------


class TestApprovalGateAuto:
    def test_auto_mode_immediately_approves(self, tmp_path: Path) -> None:
        from bernstein.core.approval import ApprovalGate, ApprovalMode

        gate = ApprovalGate(mode=ApprovalMode.AUTO, workdir=tmp_path)
        task = _make_task()
        result = gate.evaluate(task, session_id="agent-abc123")

        assert result.approved is True
        assert result.rejected is False

    def test_auto_mode_returns_no_pr_url(self, tmp_path: Path) -> None:
        from bernstein.core.approval import ApprovalGate, ApprovalMode

        gate = ApprovalGate(mode=ApprovalMode.AUTO, workdir=tmp_path)
        result = gate.evaluate(_make_task(), session_id="agent-abc123")

        assert result.pr_url == ""

    def test_auto_mode_writes_no_pending_file(self, tmp_path: Path) -> None:
        from bernstein.core.approval import ApprovalGate, ApprovalMode

        gate = ApprovalGate(mode=ApprovalMode.AUTO, workdir=tmp_path)
        gate.evaluate(_make_task(id="T-auto"), session_id="agent-auto")

        pending_dir = tmp_path / ".sdd" / "runtime" / "pending_approvals"
        assert not pending_dir.exists() or not any(pending_dir.glob("*.json"))


# ---------------------------------------------------------------------------
# ApprovalGate — review mode
# ---------------------------------------------------------------------------


class TestApprovalGateReview:
    def test_review_mode_writes_pending_file(self, tmp_path: Path) -> None:
        from bernstein.core.approval import ApprovalGate, ApprovalMode

        # Inject a mock poller that immediately returns "approved"
        gate = ApprovalGate(
            mode=ApprovalMode.REVIEW,
            workdir=tmp_path,
            _poll_decision=lambda task_id, approvals_dir: "approved",
        )
        task = _make_task(id="T-rev", title="Add auth")
        gate.evaluate(task, session_id="agent-rev", diff="diff --git ...")

        pending_file = tmp_path / ".sdd" / "runtime" / "pending_approvals" / "T-rev.json"
        assert pending_file.exists()
        data = json.loads(pending_file.read_text())
        assert data["task_id"] == "T-rev"
        assert data["task_title"] == "Add auth"

    def test_review_mode_returns_approved_when_poller_returns_approved(self, tmp_path: Path) -> None:
        from bernstein.core.approval import ApprovalGate, ApprovalMode

        gate = ApprovalGate(
            mode=ApprovalMode.REVIEW,
            workdir=tmp_path,
            _poll_decision=lambda task_id, approvals_dir: "approved",
        )
        result = gate.evaluate(_make_task(id="T-app"), session_id="agent-1")

        assert result.approved is True
        assert result.rejected is False

    def test_review_mode_returns_rejected_when_poller_returns_rejected(self, tmp_path: Path) -> None:
        from bernstein.core.approval import ApprovalGate, ApprovalMode

        gate = ApprovalGate(
            mode=ApprovalMode.REVIEW,
            workdir=tmp_path,
            _poll_decision=lambda task_id, approvals_dir: "rejected",
        )
        result = gate.evaluate(_make_task(id="T-rej"), session_id="agent-1")

        assert result.approved is False
        assert result.rejected is True

    def test_review_mode_includes_diff_in_pending_file(self, tmp_path: Path) -> None:
        from bernstein.core.approval import ApprovalGate, ApprovalMode

        gate = ApprovalGate(
            mode=ApprovalMode.REVIEW,
            workdir=tmp_path,
            _poll_decision=lambda task_id, approvals_dir: "approved",
        )
        gate.evaluate(
            _make_task(id="T-diff"),
            session_id="agent-1",
            diff="diff --git a/foo.py b/foo.py\n+print('hello')",
        )

        pending_file = tmp_path / ".sdd" / "runtime" / "pending_approvals" / "T-diff.json"
        data = json.loads(pending_file.read_text())
        assert "diff" in data
        assert "+print('hello')" in data["diff"]

    def test_review_mode_includes_test_summary_in_pending_file(self, tmp_path: Path) -> None:
        from bernstein.core.approval import ApprovalGate, ApprovalMode

        gate = ApprovalGate(
            mode=ApprovalMode.REVIEW,
            workdir=tmp_path,
            _poll_decision=lambda task_id, approvals_dir: "approved",
        )
        gate.evaluate(
            _make_task(id="T-tests"),
            session_id="agent-1",
            test_summary="12 passed, 0 failed",
        )

        pending_file = tmp_path / ".sdd" / "runtime" / "pending_approvals" / "T-tests.json"
        data = json.loads(pending_file.read_text())
        assert data.get("test_summary") == "12 passed, 0 failed"


# ---------------------------------------------------------------------------
# ApprovalGate — review mode with file-based polling
# ---------------------------------------------------------------------------


class TestApprovalGateReviewFilePoll:
    def test_default_poller_reads_approved_decision_file(self, tmp_path: Path) -> None:
        from bernstein.core.approval import _default_poll_decision

        approvals_dir = tmp_path / ".sdd" / "runtime" / "approvals"
        approvals_dir.mkdir(parents=True)
        (approvals_dir / "T-poll.approved").write_text("approved")

        decision = _default_poll_decision("T-poll", approvals_dir, poll_interval_s=0.01, max_wait_s=1.0)
        assert decision == "approved"

    def test_default_poller_reads_rejected_decision_file(self, tmp_path: Path) -> None:
        from bernstein.core.approval import _default_poll_decision

        approvals_dir = tmp_path / ".sdd" / "runtime" / "approvals"
        approvals_dir.mkdir(parents=True)
        (approvals_dir / "T-rejpoll.rejected").write_text("rejected")

        decision = _default_poll_decision("T-rejpoll", approvals_dir, poll_interval_s=0.01, max_wait_s=1.0)
        assert decision == "rejected"

    def test_default_poller_returns_approved_on_timeout(self, tmp_path: Path) -> None:
        """When no decision file appears, defaults to approved (non-blocking)."""
        from bernstein.core.approval import _default_poll_decision

        approvals_dir = tmp_path / ".sdd" / "runtime" / "approvals"
        approvals_dir.mkdir(parents=True)

        # No file written — should time out and default to approved
        decision = _default_poll_decision("T-timeout", approvals_dir, poll_interval_s=0.01, max_wait_s=0.05)
        assert decision == "approved"


# ---------------------------------------------------------------------------
# ApprovalGate — pr mode
# ---------------------------------------------------------------------------


class TestApprovalGatePR:
    def test_pr_mode_evaluate_returns_not_approved_not_rejected(self, tmp_path: Path) -> None:
        from bernstein.core.approval import ApprovalGate, ApprovalMode

        gate = ApprovalGate(mode=ApprovalMode.PR, workdir=tmp_path)
        result = gate.evaluate(_make_task(), session_id="agent-pr")

        assert result.approved is False
        assert result.rejected is False

    def test_pr_mode_evaluate_returns_pr_url_empty_until_create_called(self, tmp_path: Path) -> None:
        from bernstein.core.approval import ApprovalGate, ApprovalMode

        gate = ApprovalGate(mode=ApprovalMode.PR, workdir=tmp_path)
        result = gate.evaluate(_make_task(), session_id="agent-pr")

        assert result.pr_url == ""

    def test_create_pr_calls_git_ops_and_returns_url(self, tmp_path: Path) -> None:
        from bernstein.core.approval import ApprovalGate, ApprovalMode
        from bernstein.core.git_ops import PullRequestResult

        mock_create_pr = MagicMock(
            return_value=PullRequestResult(success=True, pr_url="https://github.com/owner/repo/pull/42")
        )
        mock_push = MagicMock(return_value=MagicMock(ok=True))

        gate = ApprovalGate(
            mode=ApprovalMode.PR,
            workdir=tmp_path,
            _create_pr_fn=mock_create_pr,
            _push_branch_fn=mock_push,
        )
        task = _make_task(id="T-pr", title="Add auth")
        pr_url = gate.create_pr(
            task,
            worktree_path=tmp_path / "worktree",
            session_id="agent-pr123",
            base_branch="master",
        )

        assert pr_url == "https://github.com/owner/repo/pull/42"
        mock_push.assert_called_once()
        mock_create_pr.assert_called_once()

    def test_create_pr_uses_bernstein_branch_prefix(self, tmp_path: Path) -> None:
        from bernstein.core.approval import ApprovalGate, ApprovalMode
        from bernstein.core.git_ops import PullRequestResult

        captured_head: list[str] = []

        def _fake_create_pr(**kwargs: object) -> PullRequestResult:
            captured_head.append(str(kwargs.get("head", "")))
            return PullRequestResult(success=True, pr_url="https://github.com/x/y/pull/1")

        mock_push = MagicMock(return_value=MagicMock(ok=True))

        gate = ApprovalGate(
            mode=ApprovalMode.PR,
            workdir=tmp_path,
            _create_pr_fn=_fake_create_pr,
            _push_branch_fn=mock_push,
        )
        gate.create_pr(
            _make_task(id="T-abc"),
            worktree_path=tmp_path / "wt",
            session_id="agent-xyz",
            base_branch="master",
        )

        assert captured_head[0].startswith("bernstein/task-")

    def test_create_pr_adds_bernstein_labels(self, tmp_path: Path) -> None:
        from bernstein.core.approval import ApprovalGate, ApprovalMode
        from bernstein.core.git_ops import PullRequestResult

        captured_labels: list[list[str]] = []

        def _fake_create_pr(**kwargs: object) -> PullRequestResult:
            captured_labels.append(list(kwargs.get("labels") or []))
            return PullRequestResult(success=True, pr_url="https://github.com/x/y/pull/2")

        mock_push = MagicMock(return_value=MagicMock(ok=True))

        gate = ApprovalGate(
            mode=ApprovalMode.PR,
            workdir=tmp_path,
            _create_pr_fn=_fake_create_pr,
            _push_branch_fn=mock_push,
        )
        gate.create_pr(
            _make_task(),
            worktree_path=tmp_path / "wt",
            session_id="agent-lab",
            base_branch="master",
        )

        assert "bernstein" in captured_labels[0]
        assert "auto-generated" in captured_labels[0]

    def test_create_pr_returns_empty_string_when_gh_fails(self, tmp_path: Path) -> None:
        from bernstein.core.approval import ApprovalGate, ApprovalMode
        from bernstein.core.git_ops import PullRequestResult

        mock_push = MagicMock(return_value=MagicMock(ok=True))

        gate = ApprovalGate(
            mode=ApprovalMode.PR,
            workdir=tmp_path,
            _create_pr_fn=MagicMock(return_value=PullRequestResult(success=False, error="gh not found")),
            _push_branch_fn=mock_push,
        )
        pr_url = gate.create_pr(
            _make_task(),
            worktree_path=tmp_path / "wt",
            session_id="agent-fail",
            base_branch="master",
        )

        assert pr_url == ""


# ---------------------------------------------------------------------------
# ApprovalResult dataclass
# ---------------------------------------------------------------------------


class TestApprovalResult:
    def test_approved_result(self) -> None:
        from bernstein.core.approval import ApprovalResult

        r = ApprovalResult(approved=True)
        assert r.approved is True
        assert r.rejected is False
        assert r.pr_url == ""

    def test_rejected_result(self) -> None:
        from bernstein.core.approval import ApprovalResult

        r = ApprovalResult(approved=False, rejected=True)
        assert r.rejected is True

    def test_pr_result_with_url(self) -> None:
        from bernstein.core.approval import ApprovalResult

        r = ApprovalResult(approved=False, rejected=False, pr_url="https://github.com/x/y/pull/7")
        assert r.pr_url == "https://github.com/x/y/pull/7"


# ---------------------------------------------------------------------------
# Spawner — skip_merge parameter
# ---------------------------------------------------------------------------


class TestSpawnerSkipMerge:
    def test_reap_completed_agent_skip_merge_does_not_merge(self, tmp_path: Path) -> None:
        """When skip_merge=True, worktree cleanup happens but no merge is attempted."""
        from bernstein.core.models import AgentSession, ModelConfig
        from bernstein.core.spawner import AgentSpawner

        from bernstein.adapters.base import CLIAdapter

        mock_adapter = MagicMock(spec=CLIAdapter)
        mock_adapter.is_rate_limited.return_value = False

        templates_dir = tmp_path / "templates" / "roles"
        templates_dir.mkdir(parents=True)
        spawner = AgentSpawner(mock_adapter, templates_dir, tmp_path, use_worktrees=False)

        # Create a mock process
        mock_proc = MagicMock()
        mock_proc.terminate.return_value = None
        mock_proc.wait.return_value = 0

        session = AgentSession(
            id="agent-skipmerge",
            role="backend",
            model_config=ModelConfig(model="sonnet", effort="high"),
        )
        spawner._procs["agent-skipmerge"] = mock_proc

        # With skip_merge=True and no worktree, should return None without merging
        result = spawner.reap_completed_agent(session, skip_merge=True)
        assert result is None

    def test_reap_completed_agent_default_behavior_unchanged(self, tmp_path: Path) -> None:
        """When skip_merge=False (default), behavior is unchanged from before."""
        from bernstein.core.models import AgentSession, ModelConfig
        from bernstein.core.spawner import AgentSpawner

        from bernstein.adapters.base import CLIAdapter

        mock_adapter = MagicMock(spec=CLIAdapter)
        mock_adapter.is_rate_limited.return_value = False

        templates_dir = tmp_path / "templates" / "roles"
        templates_dir.mkdir(parents=True)
        spawner = AgentSpawner(mock_adapter, templates_dir, tmp_path, use_worktrees=False)

        mock_proc = MagicMock()
        session = AgentSession(
            id="agent-default",
            role="backend",
            model_config=ModelConfig(model="sonnet", effort="high"),
        )
        spawner._procs["agent-default"] = mock_proc

        # No worktrees, so no merge happens regardless
        result = spawner.reap_completed_agent(session)
        assert result is None


# ---------------------------------------------------------------------------
# ApprovalGate — auto_merge and pr_labels
# ---------------------------------------------------------------------------


class TestApprovalGateAutoMerge:
    def test_auto_merge_enabled_calls_enable_pr_auto_merge(self, tmp_path: Path) -> None:
        from bernstein.core.approval import ApprovalGate, ApprovalMode
        from bernstein.core.git_ops import GitResult, PullRequestResult

        mock_push = MagicMock(return_value=MagicMock(ok=True))
        mock_create_pr = MagicMock(return_value=PullRequestResult(success=True, pr_url="https://github.com/x/y/pull/1"))
        mock_enable_auto_merge = MagicMock(return_value=GitResult(returncode=0, stdout="", stderr=""))

        with patch("bernstein.core.git_ops.enable_pr_auto_merge", mock_enable_auto_merge):
            gate = ApprovalGate(
                mode=ApprovalMode.PR,
                workdir=tmp_path,
                auto_merge=True,
                _push_branch_fn=mock_push,
                _create_pr_fn=mock_create_pr,
            )
            pr_url = gate.create_pr(
                _make_task(id="T-automerge"),
                worktree_path=tmp_path / "wt",
                session_id="agent-am",
                base_branch="master",
            )

        assert pr_url == "https://github.com/x/y/pull/1"
        mock_enable_auto_merge.assert_called_once_with(tmp_path, "https://github.com/x/y/pull/1")

    def test_auto_merge_disabled_does_not_call_enable_pr_auto_merge(self, tmp_path: Path) -> None:
        from bernstein.core.approval import ApprovalGate, ApprovalMode
        from bernstein.core.git_ops import PullRequestResult

        mock_push = MagicMock(return_value=MagicMock(ok=True))
        mock_create_pr = MagicMock(return_value=PullRequestResult(success=True, pr_url="https://github.com/x/y/pull/2"))
        mock_enable_auto_merge = MagicMock()

        with patch("bernstein.core.git_ops.enable_pr_auto_merge", mock_enable_auto_merge):
            gate = ApprovalGate(
                mode=ApprovalMode.PR,
                workdir=tmp_path,
                auto_merge=False,
                _push_branch_fn=mock_push,
                _create_pr_fn=mock_create_pr,
            )
            gate.create_pr(
                _make_task(id="T-nomerge"),
                worktree_path=tmp_path / "wt",
                session_id="agent-nm",
                base_branch="master",
            )

        mock_enable_auto_merge.assert_not_called()

    def test_custom_pr_labels_used_when_no_override(self, tmp_path: Path) -> None:
        from bernstein.core.approval import ApprovalGate, ApprovalMode
        from bernstein.core.git_ops import PullRequestResult

        captured: list[list[str]] = []

        def _fake_create_pr(**kwargs: object) -> PullRequestResult:
            captured.append(list(kwargs.get("labels") or []))
            return PullRequestResult(success=True, pr_url="https://github.com/x/y/pull/3")

        mock_push = MagicMock(return_value=MagicMock(ok=True))

        gate = ApprovalGate(
            mode=ApprovalMode.PR,
            workdir=tmp_path,
            pr_labels=["bernstein", "my-team"],
            auto_merge=False,
            _push_branch_fn=mock_push,
            _create_pr_fn=_fake_create_pr,
        )
        gate.create_pr(
            _make_task(id="T-labels"),
            worktree_path=tmp_path / "wt",
            session_id="agent-lbl",
            base_branch="master",
        )

        assert captured[0] == ["bernstein", "my-team"]

    def test_explicit_labels_override_instance_labels(self, tmp_path: Path) -> None:
        from bernstein.core.approval import ApprovalGate, ApprovalMode
        from bernstein.core.git_ops import PullRequestResult

        captured: list[list[str]] = []

        def _fake_create_pr(**kwargs: object) -> PullRequestResult:
            captured.append(list(kwargs.get("labels") or []))
            return PullRequestResult(success=True, pr_url="https://github.com/x/y/pull/4")

        mock_push = MagicMock(return_value=MagicMock(ok=True))

        gate = ApprovalGate(
            mode=ApprovalMode.PR,
            workdir=tmp_path,
            pr_labels=["bernstein"],
            auto_merge=False,
            _push_branch_fn=mock_push,
            _create_pr_fn=_fake_create_pr,
        )
        gate.create_pr(
            _make_task(id="T-override"),
            worktree_path=tmp_path / "wt",
            session_id="agent-ov",
            base_branch="master",
            labels=["override-label"],
        )

        assert captured[0] == ["override-label"]


# ---------------------------------------------------------------------------
# OrchestratorConfig — merge_strategy field
# ---------------------------------------------------------------------------


class TestOrchestratorConfigMergeStrategy:
    def test_default_merge_strategy_is_pr(self) -> None:
        cfg = OrchestratorConfig()
        assert cfg.merge_strategy == "pr"

    def test_merge_strategy_direct(self) -> None:
        cfg = OrchestratorConfig(merge_strategy="direct")
        assert cfg.merge_strategy == "direct"

    def test_auto_merge_default_true(self) -> None:
        cfg = OrchestratorConfig()
        assert cfg.auto_merge is True

    def test_pr_labels_default(self) -> None:
        cfg = OrchestratorConfig()
        assert "bernstein" in cfg.pr_labels
        assert "auto-generated" in cfg.pr_labels


class TestSpawnerGetWorktreePath:
    def test_get_worktree_path_returns_none_when_no_worktree(self, tmp_path: Path) -> None:
        from bernstein.core.spawner import AgentSpawner

        from bernstein.adapters.base import CLIAdapter

        mock_adapter = MagicMock(spec=CLIAdapter)
        mock_adapter.is_rate_limited.return_value = False
        templates_dir = tmp_path / "templates" / "roles"
        templates_dir.mkdir(parents=True)
        spawner = AgentSpawner(mock_adapter, templates_dir, tmp_path, use_worktrees=False)

        assert spawner.get_worktree_path("agent-xyz") is None

    def test_get_worktree_path_returns_path_when_registered(self, tmp_path: Path) -> None:
        from bernstein.core.spawner import AgentSpawner

        from bernstein.adapters.base import CLIAdapter

        mock_adapter = MagicMock(spec=CLIAdapter)
        mock_adapter.is_rate_limited.return_value = False
        templates_dir = tmp_path / "templates" / "roles"
        templates_dir.mkdir(parents=True)
        spawner = AgentSpawner(mock_adapter, templates_dir, tmp_path, use_worktrees=True)

        fake_path = tmp_path / ".sdd" / "worktrees" / "agent-abc"
        spawner._worktree_paths["agent-abc"] = fake_path

        assert spawner.get_worktree_path("agent-abc") == fake_path
