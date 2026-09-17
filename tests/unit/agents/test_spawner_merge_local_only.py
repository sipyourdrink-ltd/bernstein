"""A local-only run must not reach a git remote.

`_run_merge_and_push` calls `safe_push` unconditionally after a successful
merge, and `safe_push` fetches, may rebase, and writes to `origin`. On a
repository that has a remote, an offline or local-only run therefore published
agent commits nobody had reviewed -- and there was no way to ask it not to,
short of removing the remote (#5961).
"""

# pyright: reportPrivateUsage=false

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from bernstein.core.agents import spawner_merge
from bernstein.core.agents.spawner_merge import ENV_LOCAL_ONLY, _local_only


class TestLocalOnlyFlag:
    @pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on", "enable", "enabled", " 1 "])
    def test_truthy_words_opt_in(self, value: str) -> None:
        """The project's standard truthy set, same as the merge-to-default guard."""
        assert _local_only({ENV_LOCAL_ONLY: value}) is True

    @pytest.mark.parametrize("value", ["0", "false", "no", "off", ""])
    def test_everything_else_keeps_pushing(self, value: str) -> None:
        """Off by default and off for anything unrecognised: pushing is the status quo."""
        assert _local_only({ENV_LOCAL_ONLY: value}) is False

    def test_unset_keeps_pushing(self) -> None:
        assert _local_only({}) is False


class TestRetryPendingPushesRespectsLocalOnly:
    def test_no_remote_io_when_local_only(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """The retry queue is a second path to the remote, and it is guarded too."""
        monkeypatch.setenv(ENV_LOCAL_ONLY, "1")
        push = MagicMock()

        with patch("bernstein.core.git_ops.safe_push", push):
            retried = spawner_merge.retry_pending_pushes(tmp_path)

        assert retried == 0
        push.assert_not_called()

    def test_the_queue_is_left_intact(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Declining to send a queued push is not deciding it is never wanted.

        These entries were recorded by an earlier run; a local-only run must not
        consume them, or turning the flag off would silently have lost them.
        """
        queue = spawner_merge.pending_pushes_path(tmp_path)
        queue.parent.mkdir(parents=True, exist_ok=True)
        queue.write_text(f"{tmp_path}|main|agent-1\n", encoding="utf-8")
        monkeypatch.setenv(ENV_LOCAL_ONLY, "1")

        spawner_merge.retry_pending_pushes(tmp_path)

        assert queue.read_text(encoding="utf-8") == f"{tmp_path}|main|agent-1\n"


class TestMergePushRespectsLocalOnly:
    """The push after a successful merge, which is the site the issue names."""

    @staticmethod
    def _run(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, push: MagicMock) -> object:
        from bernstein.core.git_ops import MergeResult
        from bernstein.core.models import AgentSession, ModelConfig

        session = AgentSession(
            id="agent-1",
            role="backend",
            pid=1234,
            model_config=ModelConfig("sonnet", "high"),
            status="working",
        )
        # Every gate between the merge and the push is a separate concern with
        # its own tests; stub them so this one asserts only the push decision.
        monkeypatch.setattr(spawner_merge, "_blast_radius_refusal", lambda *a, **k: None)
        monkeypatch.setattr(spawner_merge, "_file_scope_refusal", lambda *a, **k: None)
        monkeypatch.setattr(spawner_merge, "_quality_gate_refusal", lambda *a, **k: None)
        monkeypatch.setattr(spawner_merge, "_record_landed_provenance", lambda *a, **k: None)
        monkeypatch.setattr(spawner_merge, "_allow_merge_to_default_branch", lambda *a, **k: True)

        merged = MergeResult(success=True, conflicting_files=[], error="")

        with (
            patch("bernstein.core.git_ops.current_branch", return_value="feature"),
            patch("bernstein.core.git_ops.protected_default_branches", return_value=frozenset()),
            patch("bernstein.core.git_ops.resolve_default_branch", return_value="main"),
            patch("bernstein.core.git_ops.safe_push", push),
        ):
            return spawner_merge._run_merge_and_push(
                session,
                tmp_path,
                lambda *a, **k: merged,
            )

    def test_push_is_skipped_when_local_only(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Skipped outright, not attempted-and-ignored.

        `safe_push` fetches and may rebase before it writes, so swallowing its
        error still performs the remote I/O an offline run cannot do.
        """
        monkeypatch.setenv(ENV_LOCAL_ONLY, "1")
        push = MagicMock()

        result = self._run(monkeypatch, tmp_path, push)

        push.assert_not_called()
        assert getattr(result, "success", None) is True, "the merge itself still happens"

    def test_push_still_happens_by_default(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """The opt-out is opt-in: an unset flag changes nothing."""
        monkeypatch.delenv(ENV_LOCAL_ONLY, raising=False)
        push = MagicMock(return_value=MagicMock(ok=True, stderr=""))

        self._run(monkeypatch, tmp_path, push)

        push.assert_called_once()
