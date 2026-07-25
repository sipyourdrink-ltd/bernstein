"""Unit tests for :mod:`bernstein.core.orchestration.commit_completion`.

Covers:

* ``CommitCompletionCheck.snapshot_before`` / ``verify_after`` against a
  real temp git repo (commit moves HEAD; idempotent reads).
* ``decide_retry`` truth table -- every reason tag has a dedicated case.
* ``adapter_supports_continuation`` reads the class attribute.
* ``build_continuation_prompt`` composes the corrective nudge.
* ``maybe_retry_continuation`` end-to-end with a stub adapter and spawn
  callable; covers the no-retry, retry-cap, and successful-retry paths.

The tests do not exercise the real ``git`` binary beyond
``git init`` / ``git commit`` smoke flow inside a ``tmp_path``. They
never touch a remote, never spawn an adapter, and never consult the
network.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from bernstein.adapters._contract import AdapterStrategy, OutputMode
from bernstein.core.orchestration.commit_completion import (
    DEFAULT_CONTINUATION_NUDGE,
    RETRY_LIMIT,
    CommitCompletionCheck,
    CompletionVerdict,
    RetryDecision,
    adapter_supports_continuation,
    build_continuation_prompt,
    decide_retry,
    maybe_retry_continuation,
    set_retry_lifecycle_emitter,
)

# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


@dataclass
class _StubAdapter:
    """Minimal adapter stand-in for the retry-decision tests.

    Only the attributes the production code reads are modelled. The
    fixture deliberately does not subclass :class:`CLIAdapter` to keep
    the test fast and free of subprocess wiring.
    """

    supports_session_continuation: bool = False
    continuation_args_value: list[str] = field(default_factory=list)
    continuation_args_calls: list[str] = field(default_factory=list)

    def continuation_args(self, session_id: str) -> list[str]:
        self.continuation_args_calls.append(session_id)
        return self.continuation_args_value.copy()


@dataclass
class _SpawnRecorder:
    """Captures the prompt + flags passed to the retry spawn."""

    calls: list[tuple[str, list[str]]] = field(default_factory=list)
    return_value: object | None = "spawn-ok"

    def __call__(self, prompt: str, continuation_args: list[str]) -> object | None:
        self.calls.append((prompt, continuation_args.copy()))
        return self.return_value


# ---------------------------------------------------------------------------
# Real git fixture
# ---------------------------------------------------------------------------


def _run_git(repo: Path, *args: str) -> str:
    env = os.environ.copy()
    env.setdefault("GIT_AUTHOR_NAME", "test")
    env.setdefault("GIT_AUTHOR_EMAIL", "test@example.invalid")
    env.setdefault("GIT_COMMITTER_NAME", "test")
    env.setdefault("GIT_COMMITTER_EMAIL", "test@example.invalid")
    proc = subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    if proc.returncode != 0:
        pytest.fail(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    _run_git(tmp_path, "init", "--initial-branch", "main", str(tmp_path))
    (tmp_path / "seed.txt").write_text("seed", encoding="utf-8")
    _run_git(tmp_path, "add", ".")
    _run_git(tmp_path, "commit", "-m", "seed")
    return tmp_path


# ---------------------------------------------------------------------------
# CommitCompletionCheck
# ---------------------------------------------------------------------------


class TestCommitCompletionCheck:
    def test_snapshot_returns_current_head(self, repo: Path) -> None:
        check = CommitCompletionCheck()
        sha = check.snapshot_before(repo)
        assert len(sha) == 40
        assert sha == _run_git(repo, "rev-parse", "HEAD")

    def test_verify_detects_no_commit(self, repo: Path) -> None:
        check = CommitCompletionCheck()
        before = check.snapshot_before(repo)
        verdict = check.verify_after(repo, before=before)
        assert verdict.committed is False
        assert verdict.needs_retry is True
        assert verdict.before == before
        assert verdict.after == before
        assert verdict.reason == "head_did_not_move"

    def test_verify_detects_commit_landed(self, repo: Path) -> None:
        check = CommitCompletionCheck()
        before = check.snapshot_before(repo)
        (repo / "new.txt").write_text("more", encoding="utf-8")
        _run_git(repo, "add", ".")
        _run_git(repo, "commit", "-m", "another")
        verdict = check.verify_after(repo, before=before)
        assert verdict.committed is True
        assert verdict.needs_retry is False
        assert verdict.after != before
        assert verdict.reason == ""

    def test_missing_workdir_yields_no_retry(self, tmp_path: Path) -> None:
        # Not a git repo -> rev_parse_head raises, returns "" -> treat as
        # committed (we cannot confidently call for a retry).
        check = CommitCompletionCheck()
        before = check.snapshot_before(tmp_path)
        assert before == ""
        verdict = check.verify_after(tmp_path, before=before)
        assert verdict.committed is True
        assert verdict.needs_retry is False
        assert verdict.reason == "head_unknown"

    def test_snapshot_is_pure_no_writes(self, repo: Path) -> None:
        before_status = _run_git(repo, "status", "--porcelain")
        check = CommitCompletionCheck()
        check.snapshot_before(repo)
        check.verify_after(repo, before=check.snapshot_before(repo))
        after_status = _run_git(repo, "status", "--porcelain")
        assert before_status == after_status


# ---------------------------------------------------------------------------
# CompletionVerdict
# ---------------------------------------------------------------------------


class TestCompletionVerdict:
    def test_needs_retry_mirror_of_committed(self) -> None:
        committed = CompletionVerdict(committed=True, before="a", after="b")
        not_committed = CompletionVerdict(committed=False, before="a", after="a")
        assert committed.needs_retry is False
        assert not_committed.needs_retry is True

    def test_frozen_dataclass(self) -> None:
        from dataclasses import FrozenInstanceError

        v = CompletionVerdict(committed=True, before="a", after="b")
        with pytest.raises(FrozenInstanceError):
            v.committed = False  # type: ignore[misc]


# ---------------------------------------------------------------------------
# decide_retry truth table
# ---------------------------------------------------------------------------


class TestDecideRetry:
    def test_non_zero_exit_blocks_retry(self) -> None:
        decision = decide_retry(
            adapter=_StubAdapter(supports_session_continuation=True),
            verdict=CompletionVerdict(committed=False, before="a", after="a", reason="head_did_not_move"),
            exit_code=2,
            attempts=0,
        )
        assert decision == RetryDecision(should_retry=False, reason="non_zero_exit")

    def test_committed_blocks_retry(self) -> None:
        decision = decide_retry(
            adapter=_StubAdapter(supports_session_continuation=True),
            verdict=CompletionVerdict(committed=True, before="a", after="b"),
            exit_code=0,
            attempts=0,
        )
        assert decision == RetryDecision(should_retry=False, reason="committed")

    def test_head_unknown_blocks_retry(self) -> None:
        decision = decide_retry(
            adapter=_StubAdapter(supports_session_continuation=True),
            verdict=CompletionVerdict(committed=True, before="", after="", reason="head_unknown"),
            exit_code=0,
            attempts=0,
        )
        assert decision == RetryDecision(should_retry=False, reason="head_unknown")

    def test_adapter_without_continuation_blocks_retry(self) -> None:
        decision = decide_retry(
            adapter=_StubAdapter(supports_session_continuation=False),
            verdict=CompletionVerdict(committed=False, before="a", after="a", reason="head_did_not_move"),
            exit_code=0,
            attempts=0,
        )
        assert decision == RetryDecision(should_retry=False, reason="adapter_unsupported")

    def test_retry_cap_blocks_second_attempt(self) -> None:
        decision = decide_retry(
            adapter=_StubAdapter(supports_session_continuation=True),
            verdict=CompletionVerdict(committed=False, before="a", after="a", reason="head_did_not_move"),
            exit_code=0,
            attempts=RETRY_LIMIT,
        )
        assert decision == RetryDecision(should_retry=False, reason="retry_cap_reached")

    def test_retry_path_when_all_conditions_met(self) -> None:
        decision = decide_retry(
            adapter=_StubAdapter(supports_session_continuation=True),
            verdict=CompletionVerdict(committed=False, before="a", after="a", reason="head_did_not_move"),
            exit_code=0,
            attempts=0,
        )
        assert decision == RetryDecision(should_retry=True, reason="needs_retry")

    def test_retry_limit_is_one(self) -> None:
        # Hard contract: the v1 cap is exactly one retry.
        assert RETRY_LIMIT == 1


# ---------------------------------------------------------------------------
# adapter_supports_continuation
# ---------------------------------------------------------------------------


class TestAdapterSupportsContinuation:
    def test_true_when_attribute_set(self) -> None:
        assert adapter_supports_continuation(_StubAdapter(supports_session_continuation=True))

    def test_false_when_attribute_absent(self) -> None:
        class _Bare:
            pass

        assert not adapter_supports_continuation(_Bare())  # type: ignore[arg-type]

    def test_false_when_attribute_false(self) -> None:
        assert not adapter_supports_continuation(_StubAdapter(supports_session_continuation=False))


# ---------------------------------------------------------------------------
# build_continuation_prompt
# ---------------------------------------------------------------------------


class TestBuildContinuationPrompt:
    def test_appends_default_nudge(self) -> None:
        out = build_continuation_prompt(original_prompt="do work")
        assert DEFAULT_CONTINUATION_NUDGE in out
        assert out.startswith("do work")
        assert "\n\n" in out

    def test_custom_nudge_replaces_default(self) -> None:
        out = build_continuation_prompt(original_prompt="task", nudge="custom-nudge")
        assert "custom-nudge" in out
        assert DEFAULT_CONTINUATION_NUDGE not in out

    def test_empty_nudge_returns_original(self) -> None:
        assert build_continuation_prompt(original_prompt="just this", nudge="") == "just this"

    def test_preserves_leading_and_trailing_whitespace(self) -> None:
        # Transcript replay must match the original spawn byte-for-byte;
        # we no longer ``strip()`` the composed prompt.
        out = build_continuation_prompt(original_prompt="  body \n", nudge="hint")
        assert out == "  body \n\n\nhint"

    def test_empty_original_returns_nudge_only(self) -> None:
        assert build_continuation_prompt(original_prompt="", nudge="just-nudge") == "just-nudge"


# ---------------------------------------------------------------------------
# maybe_retry_continuation end-to-end
# ---------------------------------------------------------------------------


class TestMaybeRetryContinuation:
    def test_no_retry_when_committed(self, repo: Path) -> None:
        check = CommitCompletionCheck()
        before = check.snapshot_before(repo)
        (repo / "new.txt").write_text("x", encoding="utf-8")
        _run_git(repo, "add", ".")
        _run_git(repo, "commit", "-m", "did work")

        adapter = _StubAdapter(supports_session_continuation=True, continuation_args_value=["--continue"])
        recorder = _SpawnRecorder()
        decision, verdict, retry_result = maybe_retry_continuation(
            adapter=adapter,  # type: ignore[arg-type]
            workdir=repo,
            before=before,
            session_id="sess-1",
            exit_code=0,
            original_prompt="prompt",
            spawn_fn=recorder,
        )
        assert decision.should_retry is False
        assert decision.reason == "committed"
        assert verdict.committed is True
        assert retry_result is None
        assert recorder.calls == []
        assert adapter.continuation_args_calls == []

    def test_retry_invokes_spawn_with_continuation_args(self, repo: Path) -> None:
        check = CommitCompletionCheck()
        before = check.snapshot_before(repo)

        adapter = _StubAdapter(supports_session_continuation=True, continuation_args_value=["--continue"])
        recorder = _SpawnRecorder(return_value="ok")
        decision, verdict, retry_result = maybe_retry_continuation(
            adapter=adapter,  # type: ignore[arg-type]
            workdir=repo,
            before=before,
            session_id="sess-2",
            exit_code=0,
            original_prompt="please ship it",
            spawn_fn=recorder,
        )
        assert decision.should_retry is True
        assert decision.reason == "needs_retry"
        assert verdict.committed is False
        assert retry_result == "ok"
        assert recorder.calls == [
            ("please ship it\n\n" + DEFAULT_CONTINUATION_NUDGE, ["--continue"]),
        ]
        assert adapter.continuation_args_calls == ["sess-2"]

    def test_retry_skipped_when_attempts_at_cap(self, repo: Path) -> None:
        check = CommitCompletionCheck()
        before = check.snapshot_before(repo)
        adapter = _StubAdapter(supports_session_continuation=True, continuation_args_value=["--continue"])
        recorder = _SpawnRecorder()
        decision, _verdict, retry_result = maybe_retry_continuation(
            adapter=adapter,  # type: ignore[arg-type]
            workdir=repo,
            before=before,
            session_id="sess-3",
            exit_code=0,
            original_prompt="x",
            attempts=RETRY_LIMIT,
            spawn_fn=recorder,
        )
        assert decision.should_retry is False
        assert decision.reason == "retry_cap_reached"
        assert retry_result is None
        assert recorder.calls == []

    def test_no_spawn_fn_returns_decision_only(self, repo: Path) -> None:
        check = CommitCompletionCheck()
        before = check.snapshot_before(repo)
        adapter = _StubAdapter(supports_session_continuation=True, continuation_args_value=["--continue"])
        decision, verdict, retry_result = maybe_retry_continuation(
            adapter=adapter,  # type: ignore[arg-type]
            workdir=repo,
            before=before,
            session_id="sess-4",
            exit_code=0,
            original_prompt="x",
            spawn_fn=None,
        )
        assert decision.should_retry is True
        assert verdict.needs_retry is True
        assert retry_result is None

    def test_no_retry_when_adapter_lacks_capability(self, repo: Path) -> None:
        check = CommitCompletionCheck()
        before = check.snapshot_before(repo)
        adapter = _StubAdapter(supports_session_continuation=False)
        recorder = _SpawnRecorder()
        decision, _verdict, retry_result = maybe_retry_continuation(
            adapter=adapter,  # type: ignore[arg-type]
            workdir=repo,
            before=before,
            session_id="sess-5",
            exit_code=0,
            original_prompt="x",
            spawn_fn=recorder,
        )
        assert decision.should_retry is False
        assert decision.reason == "adapter_unsupported"
        assert retry_result is None
        assert recorder.calls == []


# ---------------------------------------------------------------------------
# Base-class contract for adapters
# ---------------------------------------------------------------------------


class TestCLIAdapterContract:
    def test_base_adapter_defaults_to_not_supported(self) -> None:
        from bernstein.adapters.base import CLIAdapter

        assert CLIAdapter.supports_session_continuation is False

    def test_base_adapter_default_continuation_args_is_empty(self) -> None:
        from bernstein.adapters.base import CLIAdapter

        # We bypass abstract instantiation by reading the unbound method.
        unbound_args: Any = CLIAdapter.continuation_args
        # Stub self argument; method body does not consult self.
        assert unbound_args(None, "any-session") == []

    def test_claude_adapter_opts_in(self) -> None:
        from bernstein.adapters.claude import ClaudeCodeAdapter

        assert ClaudeCodeAdapter.supports_session_continuation is True
        args = ClaudeCodeAdapter.continuation_args(None, "sess-x")  # type: ignore[arg-type]
        assert args == ["--continue"]


# ---------------------------------------------------------------------------
# Lifecycle emitter
# ---------------------------------------------------------------------------


class TestLifecycleEmitter:
    def test_emits_on_retry_launch(self, repo: Path) -> None:
        captured: list[dict[str, object]] = []

        def _emit(payload: dict[str, object]) -> None:
            captured.append(payload)

        set_retry_lifecycle_emitter(_emit)
        try:
            check = CommitCompletionCheck()
            before = check.snapshot_before(repo)
            adapter = _StubAdapter(supports_session_continuation=True, continuation_args_value=["--continue"])
            recorder = _SpawnRecorder()
            maybe_retry_continuation(
                adapter=adapter,  # type: ignore[arg-type]
                workdir=repo,
                before=before,
                session_id="sess-emit",
                exit_code=0,
                original_prompt="p",
                spawn_fn=recorder,
            )
        finally:
            set_retry_lifecycle_emitter(None)

        assert captured == [{"session_id": "sess-emit", "reason": "needs_retry", "attempt": 1}]

    def test_no_emit_when_decision_skips_retry(self, repo: Path) -> None:
        captured: list[dict[str, object]] = []

        def _emit(payload: dict[str, object]) -> None:
            captured.append(payload)

        set_retry_lifecycle_emitter(_emit)
        try:
            check = CommitCompletionCheck()
            before = check.snapshot_before(repo)
            (repo / "y.txt").write_text("y", encoding="utf-8")
            _run_git(repo, "add", ".")
            _run_git(repo, "commit", "-m", "did it")
            adapter = _StubAdapter(supports_session_continuation=True, continuation_args_value=["--continue"])
            recorder = _SpawnRecorder()
            maybe_retry_continuation(
                adapter=adapter,  # type: ignore[arg-type]
                workdir=repo,
                before=before,
                session_id="sess-no-emit",
                exit_code=0,
                original_prompt="p",
                spawn_fn=recorder,
            )
        finally:
            set_retry_lifecycle_emitter(None)

        assert captured == []

    def test_emitter_failure_does_not_break_retry(self, repo: Path) -> None:
        def _boom(_payload: dict[str, object]) -> None:
            raise RuntimeError("downstream registry exploded")

        set_retry_lifecycle_emitter(_boom)
        try:
            check = CommitCompletionCheck()
            before = check.snapshot_before(repo)
            adapter = _StubAdapter(supports_session_continuation=True, continuation_args_value=["--continue"])
            recorder = _SpawnRecorder(return_value="spawned")
            decision, _verdict, result = maybe_retry_continuation(
                adapter=adapter,  # type: ignore[arg-type]
                workdir=repo,
                before=before,
                session_id="sess-boom",
                exit_code=0,
                original_prompt="p",
                spawn_fn=recorder,
            )
        finally:
            set_retry_lifecycle_emitter(None)

        assert decision.should_retry is True
        assert result == "spawned"
        assert recorder.calls != []


# ---------------------------------------------------------------------------
# Output-mode axis (issue #2608)
# ---------------------------------------------------------------------------


class _ArtifactModeAdapter(_StubAdapter):
    """Stub whose declared strategy is artifact output mode."""

    def strategy(self) -> AdapterStrategy:
        return AdapterStrategy(output_mode=OutputMode.ARTIFACT)


class _GitDiffAdapter(_StubAdapter):
    """Stub whose declared strategy is the default git-diff output mode."""

    def strategy(self) -> AdapterStrategy:
        return AdapterStrategy()


class TestOutputModeGatesTheCommitCheck:
    """An artifact-mode run has no commit, so an unmoved HEAD is not a defect."""

    def test_artifact_mode_never_nudges_for_a_missing_commit(self) -> None:
        adapter = _ArtifactModeAdapter(supports_session_continuation=True)
        decision = decide_retry(
            adapter=adapter,  # type: ignore[arg-type]
            verdict=CompletionVerdict(committed=False, before="a" * 40, after="a" * 40, reason="head_did_not_move"),
            exit_code=0,
            attempts=0,
        )
        assert decision == RetryDecision(should_retry=False, reason="artifact_output_mode")

    def test_task_level_override_beats_the_adapter_declaration(self) -> None:
        """One adapter drives both a coding task and a report task."""
        adapter = _GitDiffAdapter(supports_session_continuation=True)
        verdict = CompletionVerdict(committed=False, before="a" * 40, after="a" * 40, reason="head_did_not_move")

        assert (
            decide_retry(
                adapter=adapter,  # type: ignore[arg-type]
                verdict=verdict,
                exit_code=0,
                attempts=0,
            ).should_retry
            is True
        )

        assert decide_retry(
            adapter=adapter,  # type: ignore[arg-type]
            verdict=verdict,
            exit_code=0,
            attempts=0,
            output_mode=OutputMode.ARTIFACT,
        ) == RetryDecision(should_retry=False, reason="artifact_output_mode")

    def test_git_diff_mode_keeps_the_existing_truth_table(self) -> None:
        adapter = _GitDiffAdapter(supports_session_continuation=True)
        decision = decide_retry(
            adapter=adapter,  # type: ignore[arg-type]
            verdict=CompletionVerdict(committed=False, before="a" * 40, after="a" * 40, reason="head_did_not_move"),
            exit_code=0,
            attempts=0,
            output_mode=OutputMode.GIT_DIFF,
        )
        assert decision == RetryDecision(should_retry=True, reason="needs_retry")

    def test_a_non_zero_exit_still_outranks_the_output_mode(self) -> None:
        """Failure handling owns a crashed run whatever it was producing."""
        decision = decide_retry(
            adapter=_ArtifactModeAdapter(supports_session_continuation=True),  # type: ignore[arg-type]
            verdict=CompletionVerdict(committed=False, before="a" * 40, after="a" * 40, reason="head_did_not_move"),
            exit_code=1,
            attempts=0,
        )
        assert decision == RetryDecision(should_retry=False, reason="non_zero_exit")

    def test_an_adapter_without_a_strategy_defaults_to_git_diff(self) -> None:
        """A stub with no ``strategy()`` must not silently skip the check."""
        decision = decide_retry(
            adapter=_StubAdapter(supports_session_continuation=True),  # type: ignore[arg-type]
            verdict=CompletionVerdict(committed=False, before="a" * 40, after="a" * 40, reason="head_did_not_move"),
            exit_code=0,
            attempts=0,
        )
        assert decision == RetryDecision(should_retry=True, reason="needs_retry")

    def test_maybe_retry_continuation_forwards_the_override(self, repo: Path) -> None:
        """The convenience wrapper honours a task-level artifact declaration."""
        check = CommitCompletionCheck()
        before = check.snapshot_before(repo)
        recorder = _SpawnRecorder(return_value="spawned")
        decision, _verdict, result = maybe_retry_continuation(
            adapter=_GitDiffAdapter(supports_session_continuation=True, continuation_args_value=["--continue"]),  # type: ignore[arg-type]
            workdir=repo,
            before=before,
            session_id="sess-artifact",
            exit_code=0,
            original_prompt="p",
            spawn_fn=recorder,
            output_mode=OutputMode.ARTIFACT,
        )
        assert decision == RetryDecision(should_retry=False, reason="artifact_output_mode")
        assert result is None
        assert recorder.calls == []
