"""``LifecycleEvent.POST_MERGE`` fires from the merge-back path (#5962).

The event was declared, and the notification bridge subscribed to it, but
``spawner_merge`` never dispatched it, so a ``post_merge`` hook or sink
could be configured and would never run. These tests drive the real merge
path over a real repository and assert the event reaches the process-wide
registry with the merged branch, task id and the commit that landed.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from bernstein.core.agents.spawner_merge import _run_merge_and_push
from bernstein.core.lifecycle import hooks as lifecycle_hooks
from bernstein.core.lifecycle.hooks import HookDenied, HookRegistry, LifecycleContext, LifecycleEvent


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=True).stdout.strip()


class _Session:
    """Minimal stand-in for ``AgentSession`` on the merge path."""

    def __init__(self, session_id: str = "sess-1") -> None:
        self.id = session_id
        self.task_ids: list[str] = ["task-1", "task-2"]
        self.role = "worker"


class _MergeResult:
    def __init__(self, success: bool) -> None:
        self.success = success
        self.conflicting_files: list[str] = []
        self.error = ""


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A repo on a non-default branch with ``agent/sess-1`` ready to merge."""
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "T")
    (root / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "seed")

    _git(root, "checkout", "-q", "-b", "agent/sess-1")
    (root / "feature.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "agent work")

    _git(root, "checkout", "-q", "-b", "integration", "main")

    key_path = tmp_path / "audit.key"
    key_path.write_bytes(b"k" * 32)
    key_path.chmod(0o600)
    monkeypatch.setenv("BERNSTEIN_AUDIT_KEY_PATH", str(key_path))
    return root


@pytest.fixture
def registry(monkeypatch: pytest.MonkeyPatch) -> HookRegistry:
    """Install a fresh process-wide registry for the duration of a test."""
    reg = HookRegistry()
    monkeypatch.setattr(lifecycle_hooks, "GLOBAL_REGISTRY", reg, raising=False)
    return reg


def _merge_fn(success: bool = True) -> Any:
    def _merge(session_id: str, repo_root: Path) -> _MergeResult:
        if success:
            _git(repo_root, "merge", "--no-ff", "-q", "-m", "merge agent work", f"agent/{session_id}")
        return _MergeResult(success=success)

    return _merge


def _record(registry: HookRegistry) -> list[LifecycleContext]:
    seen: list[LifecycleContext] = []
    registry.register_callable(LifecycleEvent.POST_MERGE, seen.append)
    return seen


def test_post_merge_event_dispatched_after_merge(repo: Path, registry: HookRegistry) -> None:
    seen = _record(registry)

    result = _run_merge_and_push(_Session(), repo, _merge_fn())  # type: ignore[arg-type]

    assert result is not None and result.success
    assert len(seen) == 1
    ctx = seen[0]
    assert ctx.event == LifecycleEvent.POST_MERGE
    assert ctx.task == "task-1"
    assert ctx.session_id == "sess-1"
    assert ctx.workdir == repo
    assert ctx.data == {
        "branch": "agent/sess-1",
        "merged_into": "integration",
        "commit_sha": _git(repo, "rev-parse", "HEAD"),
        "task_ids": ["task-1", "task-2"],
    }
    # The sha is the merge commit, not the base the merge started from.
    assert _git(repo, "log", "-1", "--pretty=%s", ctx.data["commit_sha"]) == "merge agent work"


def test_a_failed_merge_dispatches_nothing(repo: Path, registry: HookRegistry) -> None:
    seen = _record(registry)

    result = _run_merge_and_push(_Session(), repo, _merge_fn(success=False))  # type: ignore[arg-type]

    assert result is not None and not result.success
    assert seen == []


def test_a_refused_merge_dispatches_nothing(repo: Path, registry: HookRegistry) -> None:
    """The default-branch refusal returns before the merge, so no event."""
    seen = _record(registry)
    _git(repo, "checkout", "-q", "main")

    result = _run_merge_and_push(_Session(), repo, _merge_fn())  # type: ignore[arg-type]

    assert result is not None and not result.success
    assert seen == []


@pytest.mark.parametrize("exc", [RuntimeError("sink down"), HookDenied(LifecycleEvent.POST_MERGE, "test", "no")])
def test_a_raising_hook_does_not_undo_or_fail_the_merge(repo: Path, registry: HookRegistry, exc: Exception) -> None:
    """The merge has landed by the time the event fires; a hook cannot take it back."""

    def _boom(_ctx: LifecycleContext) -> None:
        raise exc

    registry.register_callable(LifecycleEvent.POST_MERGE, _boom)

    result = _run_merge_and_push(_Session(), repo, _merge_fn())  # type: ignore[arg-type]

    assert result is not None and result.success
    assert _git(repo, "log", "-1", "--pretty=%s") == "merge agent work"


def test_no_installed_registry_is_a_no_op(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(lifecycle_hooks, "GLOBAL_REGISTRY", None, raising=False)

    result = _run_merge_and_push(_Session(), repo, _merge_fn())  # type: ignore[arg-type]

    assert result is not None and result.success
