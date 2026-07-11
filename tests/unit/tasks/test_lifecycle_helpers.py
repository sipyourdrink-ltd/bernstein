"""Behavioral tests for pure helper logic in ``task_lifecycle``.

These cover the retry-escalation ladder, dynamic retry limits, batch timeout
bucketing, file-overlap detection, path inference, decomposition gating, and
llm-judge routing. All functions under test are pure (no orchestrator, no I/O)
so each assertion checks a concrete returned value.
"""

from __future__ import annotations

import time

import pytest

from bernstein.core.tasks.models import (
    AgentSession,
    CompletionSignal,
    Complexity,
    Scope,
    Task,
)
from bernstein.core.tasks.task_lifecycle import (
    _batch_timeout_seconds,
    _bump_effort,
    _choose_retry_escalation,
    _dynamic_retry_limit,
    _escalate_model,
    _has_llm_judge_signal,
    check_file_overlap,
    infer_affected_paths,
    should_auto_decompose,
)


def _task(**overrides: object) -> Task:
    base: dict[str, object] = {
        "id": "t-1",
        "title": "Title",
        "description": "Description",
        "role": "backend",
    }
    base.update(overrides)
    return Task(**base)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# _bump_effort
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("current", "expected"),
    [
        ("low", "medium"),
        ("medium", "high"),
        ("high", "max"),
        ("max", "max"),  # capped at top
    ],
)
def test_bump_effort_walks_ladder(current: str, expected: str) -> None:
    assert _bump_effort(current) == expected


def test_bump_effort_unknown_value_starts_at_high_position() -> None:
    # Unknown effort defaults to index 2 ("high"), so the next is "max".
    assert _bump_effort("totally-unknown") == "max"


# ---------------------------------------------------------------------------
# _escalate_model
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("current", "expected"),
    [
        ("haiku", "sonnet"),
        ("sonnet", "opus"),
        ("opus", "opus"),  # capped at top
    ],
)
def test_escalate_model_walks_ladder(current: str, expected: str) -> None:
    assert _escalate_model(current) == expected


def test_escalate_model_matches_substring_case_insensitive() -> None:
    # A fully-qualified model id containing "sonnet" maps to the sonnet rung.
    assert _escalate_model("claude-3-5-SONNET-latest") == "opus"


def test_escalate_model_unknown_defaults_to_sonnet_position() -> None:
    # No ladder match -> default index 1 (sonnet) -> escalate to opus.
    assert _escalate_model("gpt-5-mini") == "opus"


# ---------------------------------------------------------------------------
# _choose_retry_escalation
# ---------------------------------------------------------------------------


def test_retry_escalation_max_turns_bumps_effort_keeps_model() -> None:
    task = _task(terminal_reason="error_max_turns")
    assert _choose_retry_escalation(task, 1, "sonnet", "medium") == ("sonnet", "high")


def test_retry_escalation_max_turns_keeps_effort_when_already_max() -> None:
    task = _task(terminal_reason="error_max_turns")
    assert _choose_retry_escalation(task, 1, "sonnet", "max") == ("sonnet", "max")


def test_retry_escalation_budget_forces_max_effort() -> None:
    task = _task(terminal_reason="error_max_budget_usd")
    assert _choose_retry_escalation(task, 1, "haiku", "low") == ("haiku", "max")


def test_retry_escalation_model_error_keeps_both() -> None:
    task = _task(terminal_reason="model_error")
    assert _choose_retry_escalation(task, 3, "sonnet", "medium") == ("sonnet", "medium")


def test_retry_escalation_blocking_limit_jumps_to_opus_max() -> None:
    task = _task(terminal_reason="blocking_limit")
    assert _choose_retry_escalation(task, 1, "haiku", "low") == ("opus", "max")


def test_retry_escalation_large_scope_jumps_to_opus_max() -> None:
    task = _task(scope=Scope.LARGE)
    assert _choose_retry_escalation(task, 1, "sonnet", "low") == ("opus", "max")


@pytest.mark.parametrize("role", ["architect", "security"])
def test_retry_escalation_high_value_roles_jump_to_opus_max(role: str) -> None:
    task = _task(role=role)
    assert _choose_retry_escalation(task, 1, "sonnet", "low") == ("opus", "max")


def test_retry_escalation_past_deadline_jumps_to_opus_max() -> None:
    task = _task(deadline=time.time() - 100)
    assert _choose_retry_escalation(task, 1, "sonnet", "low") == ("opus", "max")


def test_retry_escalation_first_retry_bumps_effort_only() -> None:
    task = _task()
    assert _choose_retry_escalation(task, 1, "sonnet", "low") == ("sonnet", "medium")


def test_retry_escalation_second_retry_escalates_model_resets_effort_high() -> None:
    task = _task()
    assert _choose_retry_escalation(task, 2, "haiku", "low") == ("sonnet", "high")


def test_retry_escalation_terminal_reason_takes_precedence_over_scope() -> None:
    # blocking_limit short-circuits before the LARGE-scope branch is reached;
    # both would produce ("opus", "max") here, so use model_error to prove it.
    task = _task(scope=Scope.LARGE, terminal_reason="model_error")
    # model_error returns current model/effort unchanged, NOT opus/max from scope.
    assert _choose_retry_escalation(task, 1, "haiku", "low") == ("haiku", "low")


# ---------------------------------------------------------------------------
# _dynamic_retry_limit
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "reason",
    [
        "Connection timeout after 30s",
        "HTTP 503 Service Unavailable",
        "rate limit exceeded",
        "429 Too Many Requests",
        "API overloaded, retry later",
        "transient network error",
    ],
)
def test_dynamic_retry_limit_transient_markers_return_three(reason: str) -> None:
    # Transient failures get a generous retry budget regardless of the default.
    assert _dynamic_retry_limit(reason, default_max=0) == 3


@pytest.mark.parametrize(
    "reason",
    [
        "SyntaxError: invalid syntax",
        "syntax error in module",
        "fatal: could not read from remote",
    ],
)
def test_dynamic_retry_limit_fatal_markers_return_zero(reason: str) -> None:
    # Fatal failures are non-retryable even when the default allows retries.
    assert _dynamic_retry_limit(reason, default_max=5) == 0


def test_dynamic_retry_limit_unknown_reason_uses_default() -> None:
    assert _dynamic_retry_limit("some unfamiliar failure", default_max=7) == 7


def test_dynamic_retry_limit_transient_beats_default_higher() -> None:
    # Even if default is higher than 3, a transient marker clamps to 3.
    assert _dynamic_retry_limit("connection refused", default_max=9) == 3


@pytest.mark.parametrize(
    "reason",
    [
        # A terminal gate-refusal reason (issue #2341): the random compaction
        # correlation hex happens to embed a numeric status marker. This must
        # NOT be read as a transient failure and granted a retry budget.
        "Context overflow: compaction refused by sensitive gate "
        "(rules: content.pem-unterminated; correlation=compact-a1503f2b)",
        "correlation=compact-de429abc",  # '429' inside a hex run
        "correlation=compact-9502dead",  # '502' inside a hex run
        "correlation=compact-fa504bcd",  # '504' inside a hex run
    ],
)
def test_dynamic_retry_limit_status_digits_inside_opaque_id_are_not_transient(reason: str) -> None:
    # Regression (#2341): a status-shaped digit run buried in an identifier
    # aliases a transient marker only under bare-substring matching. With
    # token-boundary matching it stays terminal, so a refusal keeps its
    # forced no-retry budget instead of intermittently gaining three retries.
    assert _dynamic_retry_limit(reason, default_max=0) == 0


@pytest.mark.parametrize(
    "reason",
    [
        "HTTP 503 Service Unavailable",
        "received 429 from provider",
        "gateway returned 502",
        "upstream 504 gateway timeout",
    ],
)
def test_dynamic_retry_limit_real_http_status_still_transient(reason: str) -> None:
    # The boundary-matching fix must not regress genuine status markers that
    # appear as standalone tokens in a real provider error string.
    assert _dynamic_retry_limit(reason, default_max=0) == 3


# ---------------------------------------------------------------------------
# _batch_timeout_seconds
# ---------------------------------------------------------------------------


def test_batch_timeout_small_scope_is_15_minutes() -> None:
    batch = [_task(scope=Scope.SMALL)]
    assert _batch_timeout_seconds(batch) == 900


def test_batch_timeout_medium_scope_is_30_minutes() -> None:
    batch = [_task(scope=Scope.MEDIUM)]
    assert _batch_timeout_seconds(batch) == 1800


def test_batch_timeout_takes_max_bucket_across_batch() -> None:
    batch = [_task(scope=Scope.SMALL), _task(scope=Scope.MEDIUM)]
    assert _batch_timeout_seconds(batch) == 1800


def test_batch_timeout_xl_role_promotes_to_xl_timeout() -> None:
    # An XL role (architect/security/manager) forces the xl bucket.
    batch = [_task(scope=Scope.SMALL, role="architect")]
    assert _batch_timeout_seconds(batch) == 7200


def test_batch_timeout_large_high_complexity_promotes_to_xl() -> None:
    batch = [_task(scope=Scope.LARGE, complexity=Complexity.HIGH)]
    assert _batch_timeout_seconds(batch) == 7200


def test_batch_timeout_large_low_complexity_stays_large_bucket() -> None:
    batch = [_task(scope=Scope.LARGE, complexity=Complexity.LOW)]
    assert _batch_timeout_seconds(batch) == 3600


# ---------------------------------------------------------------------------
# infer_affected_paths
# ---------------------------------------------------------------------------


def test_infer_affected_paths_extracts_explicit_src_and_test_paths() -> None:
    task = _task(
        title="Fix src/bernstein/core/foo.py",
        description="Also touch tests/unit/test_bar.py please",
    )
    assert infer_affected_paths(task) == {
        "src/bernstein/core/foo.py",
        "tests/unit/test_bar.py",
    }


def test_infer_affected_paths_empty_when_no_paths_mentioned() -> None:
    task = _task(title="Refactor the planner", description="No file paths here.")
    assert infer_affected_paths(task) == set()


def test_infer_affected_paths_does_not_double_count_qualified_path() -> None:
    # The bare-name resolver skips a name already present as a full path.
    task = _task(title="Edit src/bernstein/core/defaults.py", description="defaults.py")
    paths = infer_affected_paths(task)
    assert "src/bernstein/core/defaults.py" in paths
    # The bare "defaults.py" must not add a second, different entry.
    assert all(p.endswith("defaults.py") for p in paths)


# ---------------------------------------------------------------------------
# check_file_overlap
# ---------------------------------------------------------------------------


def _agent(agent_id: str, status: str = "working") -> AgentSession:
    sess = AgentSession(id=agent_id, role="backend")
    sess.status = status  # type: ignore[assignment]
    return sess


def test_check_file_overlap_detects_conflict_with_live_agent() -> None:
    task = _task(owned_files=["src/a.py"])
    ownership = {"src/a.py": "agent-1"}
    agents = {"agent-1": _agent("agent-1", "working")}
    assert check_file_overlap([task], ownership, agents) is True


def test_check_file_overlap_ignores_dead_agent_ownership() -> None:
    task = _task(owned_files=["src/a.py"])
    ownership = {"src/a.py": "agent-1"}
    agents = {"agent-1": _agent("agent-1", "dead")}
    assert check_file_overlap([task], ownership, agents) is False


def test_check_file_overlap_no_conflict_when_files_disjoint() -> None:
    task = _task(owned_files=["src/b.py"])
    ownership = {"src/a.py": "agent-1"}
    agents = {"agent-1": _agent("agent-1", "working")}
    assert check_file_overlap([task], ownership, agents) is False


def test_check_file_overlap_missing_owner_session_is_not_conflict() -> None:
    # File owned by an agent that is no longer in the agents dict -> no conflict.
    task = _task(owned_files=["src/a.py"])
    ownership = {"src/a.py": "ghost-agent"}
    agents: dict[str, AgentSession] = {}
    assert check_file_overlap([task], ownership, agents) is False


def test_check_file_overlap_uses_inferred_paths_from_text() -> None:
    # No explicit owned_files, but the description names a file owned by a live agent.
    task = _task(description="please edit src/bernstein/core/foo.py")
    ownership = {"src/bernstein/core/foo.py": "agent-1"}
    agents = {"agent-1": _agent("agent-1", "working")}
    assert check_file_overlap([task], ownership, agents) is True


# ---------------------------------------------------------------------------
# should_auto_decompose
# ---------------------------------------------------------------------------


def test_should_auto_decompose_disabled_without_force_parallel() -> None:
    task = _task(scope=Scope.LARGE)
    assert should_auto_decompose(task, set(), force_parallel=False) is False


def test_should_auto_decompose_large_scope_when_forced() -> None:
    task = _task(scope=Scope.LARGE)
    assert should_auto_decompose(task, set(), force_parallel=True) is True


def test_should_auto_decompose_small_scope_no_retries_is_false() -> None:
    task = _task(scope=Scope.SMALL, retry_count=0)
    assert should_auto_decompose(task, set(), force_parallel=True) is False


def test_should_auto_decompose_two_retries_triggers() -> None:
    task = _task(scope=Scope.SMALL, retry_count=2)
    assert should_auto_decompose(task, set(), force_parallel=True) is True


def test_should_auto_decompose_skips_already_decomposed() -> None:
    task = _task(id="dt-1", scope=Scope.LARGE)
    assert should_auto_decompose(task, {"dt-1"}, force_parallel=True) is False


def test_should_auto_decompose_skips_decompose_prefixed_title() -> None:
    task = _task(scope=Scope.LARGE, title="[DECOMPOSE] subtask body")
    assert should_auto_decompose(task, set(), force_parallel=True) is False


def test_should_auto_decompose_reads_legacy_retry_prefix_when_count_zero() -> None:
    # Pre-migration tasks carry "[RETRY N]" in the title with retry_count==0.
    task = _task(scope=Scope.SMALL, retry_count=0, title="[RETRY 2] do the thing")
    assert should_auto_decompose(task, set(), force_parallel=True) is True


def test_should_auto_decompose_legacy_retry_one_is_not_enough() -> None:
    task = _task(scope=Scope.SMALL, retry_count=0, title="[RETRY 1] do the thing")
    assert should_auto_decompose(task, set(), force_parallel=True) is False


# ---------------------------------------------------------------------------
# _has_llm_judge_signal
# ---------------------------------------------------------------------------


def test_has_llm_judge_signal_true_when_present() -> None:
    task = _task(completion_signals=[CompletionSignal(type="llm_judge", value="review")])
    assert _has_llm_judge_signal(task) is True


def test_has_llm_judge_signal_false_for_other_signal_types() -> None:
    task = _task(completion_signals=[CompletionSignal(type="path_exists", value="x")])
    assert _has_llm_judge_signal(task) is False


def test_has_llm_judge_signal_false_when_no_signals() -> None:
    task = _task(completion_signals=[])
    assert _has_llm_judge_signal(task) is False


def test_has_llm_judge_signal_true_when_mixed_with_others() -> None:
    task = _task(
        completion_signals=[
            CompletionSignal(type="path_exists", value="x"),
            CompletionSignal(type="llm_judge", value="review"),
        ]
    )
    assert _has_llm_judge_signal(task) is True
