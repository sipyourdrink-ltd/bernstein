"""Tests for prompt/model canary deployment: hashing, routing, evaluation."""

from __future__ import annotations

from pathlib import Path

import pytest

from bernstein.core.config.prompt_versions import (
    CanaryState,
    PromptVersion,
    create_prompt_version,
    evaluate_canary,
    hash_prompt,
    load_canary_state,
    promote_canary,
    record_result,
    rollback_canary,
    save_canary_state,
    should_route_to_canary,
    version_id,
)

# ---------------------------------------------------------------------------
# hash_prompt / version_id
# ---------------------------------------------------------------------------


def test_hash_prompt_is_deterministic() -> None:
    assert hash_prompt("hello world") == hash_prompt("hello world")


def test_hash_prompt_differs_for_different_content() -> None:
    assert hash_prompt("a") != hash_prompt("b")


def test_hash_prompt_is_hex_sha256() -> None:
    result = hash_prompt("some prompt")
    assert len(result) == 64
    int(result, 16)  # valid hex


def test_version_id_is_twelve_chars() -> None:
    h = hash_prompt("anything")
    assert len(version_id(h)) == 12


def test_version_id_is_prefix_of_hash() -> None:
    h = hash_prompt("anything")
    assert h.startswith(version_id(h))


# ---------------------------------------------------------------------------
# create_prompt_version
# ---------------------------------------------------------------------------


def test_create_prompt_version_shape() -> None:
    pv = create_prompt_version("backend", "do X", notes="v1")
    assert isinstance(pv, PromptVersion)
    assert pv.role == "backend"
    assert pv.content == "do X"
    assert pv.notes == "v1"
    assert pv.prompt_hash == hash_prompt("do X")
    assert pv.version_id == version_id(pv.prompt_hash)
    assert pv.created_at > 0


def test_create_prompt_version_default_notes() -> None:
    pv = create_prompt_version("qa", "check Y")
    assert pv.notes == ""


def test_create_prompt_version_identical_content_yields_identical_id() -> None:
    a = create_prompt_version("role", "content")
    b = create_prompt_version("role", "content")
    assert a.version_id == b.version_id
    assert a.prompt_hash == b.prompt_hash


# ---------------------------------------------------------------------------
# save / load canary state
# ---------------------------------------------------------------------------


def test_save_load_roundtrip(tmp_path: Path) -> None:
    state = CanaryState(
        stable_version="abc123",
        canary_version="def456",
        canary_percentage=25,
        canary_task_count=5,
        stable_task_count=15,
        canary_pass_count=4,
        stable_pass_count=14,
    )
    save_canary_state(state, tmp_path)
    loaded = load_canary_state(tmp_path)
    assert loaded == state


def test_load_returns_none_when_missing(tmp_path: Path) -> None:
    assert load_canary_state(tmp_path) is None


def test_save_creates_parent_dir(tmp_path: Path) -> None:
    nested = tmp_path / "deeply" / "nested"
    state = CanaryState(stable_version="x")
    save_canary_state(state, nested)
    assert (nested / "canary.json").exists()


# ---------------------------------------------------------------------------
# should_route_to_canary
# ---------------------------------------------------------------------------


def test_route_zero_percent() -> None:
    state = CanaryState(stable_version="s", canary_version="c", canary_percentage=0)
    assert not any(should_route_to_canary(state, i) for i in range(100))


def test_route_hundred_percent() -> None:
    state = CanaryState(stable_version="s", canary_version="c", canary_percentage=100)
    assert all(should_route_to_canary(state, i) for i in range(100))


def test_route_twenty_percent() -> None:
    state = CanaryState(stable_version="s", canary_version="c", canary_percentage=20)
    routed = [should_route_to_canary(state, i) for i in range(100)]
    assert sum(routed) == 20
    # First 20 indices route to canary under the modulo scheme
    assert all(routed[:20])
    assert not any(routed[20:])


def test_route_requires_canary_version() -> None:
    state = CanaryState(stable_version="s", canary_version="", canary_percentage=50)
    assert not should_route_to_canary(state, 0)


def test_route_deterministic_across_calls() -> None:
    state = CanaryState(stable_version="s", canary_version="c", canary_percentage=30)
    first = [should_route_to_canary(state, i) for i in range(50)]
    second = [should_route_to_canary(state, i) for i in range(50)]
    assert first == second


# ---------------------------------------------------------------------------
# record_result
# ---------------------------------------------------------------------------


def test_record_result_canary_pass() -> None:
    state = CanaryState(stable_version="s", canary_version="c")
    record_result(state, used_canary=True, passed=True)
    assert state.canary_task_count == 1
    assert state.canary_pass_count == 1
    assert state.stable_task_count == 0


def test_record_result_canary_fail() -> None:
    state = CanaryState(stable_version="s", canary_version="c")
    record_result(state, used_canary=True, passed=False)
    assert state.canary_task_count == 1
    assert state.canary_pass_count == 0


def test_record_result_stable_pass() -> None:
    state = CanaryState(stable_version="s")
    record_result(state, used_canary=False, passed=True)
    assert state.stable_task_count == 1
    assert state.stable_pass_count == 1


def test_record_result_stable_fail() -> None:
    state = CanaryState(stable_version="s")
    record_result(state, used_canary=False, passed=False)
    assert state.stable_task_count == 1
    assert state.stable_pass_count == 0


def test_record_result_accumulates() -> None:
    state = CanaryState(stable_version="s", canary_version="c")
    for _ in range(5):
        record_result(state, used_canary=True, passed=True)
    for _ in range(3):
        record_result(state, used_canary=False, passed=True)
    assert state.canary_task_count == 5
    assert state.canary_pass_count == 5
    assert state.stable_task_count == 3
    assert state.stable_pass_count == 3


# ---------------------------------------------------------------------------
# evaluate_canary
# ---------------------------------------------------------------------------


def test_evaluate_continue_below_threshold() -> None:
    state = CanaryState(
        stable_version="s",
        canary_version="c",
        canary_task_count=5,
        auto_promote_threshold=10,
    )
    assert evaluate_canary(state) == "continue"


def test_evaluate_promote_when_canary_matches_stable() -> None:
    state = CanaryState(
        stable_version="s",
        canary_version="c",
        canary_task_count=10,
        canary_pass_count=9,
        stable_task_count=10,
        stable_pass_count=9,
        auto_promote_threshold=10,
    )
    assert evaluate_canary(state) == "promote"


def test_evaluate_promote_when_canary_better() -> None:
    state = CanaryState(
        stable_version="s",
        canary_version="c",
        canary_task_count=10,
        canary_pass_count=10,
        stable_task_count=10,
        stable_pass_count=7,
        auto_promote_threshold=10,
    )
    assert evaluate_canary(state) == "promote"


def test_evaluate_rollback_when_canary_much_worse() -> None:
    state = CanaryState(
        stable_version="s",
        canary_version="c",
        canary_task_count=10,
        canary_pass_count=5,  # 50%
        stable_task_count=10,
        stable_pass_count=10,  # 100% -> 50% diff, > 10% threshold
        auto_promote_threshold=10,
        auto_rollback_diff_pct=10.0,
    )
    assert evaluate_canary(state) == "rollback"


def test_evaluate_continue_when_slightly_worse_under_threshold() -> None:
    state = CanaryState(
        stable_version="s",
        canary_version="c",
        canary_task_count=10,
        canary_pass_count=9,  # 90%
        stable_task_count=10,
        stable_pass_count=10,  # 100% -> 10% diff, NOT > 10%
        auto_promote_threshold=10,
        auto_rollback_diff_pct=10.0,
    )
    assert evaluate_canary(state) == "continue"


def test_evaluate_no_stable_data_defaults_to_promote() -> None:
    # When there are no stable samples, stable_rate defers to canary_rate,
    # so canary >= stable trivially and we should promote.
    state = CanaryState(
        stable_version="s",
        canary_version="c",
        canary_task_count=10,
        canary_pass_count=8,
        stable_task_count=0,
        stable_pass_count=0,
        auto_promote_threshold=10,
    )
    assert evaluate_canary(state) == "promote"


# ---------------------------------------------------------------------------
# promote / rollback
# ---------------------------------------------------------------------------


def test_promote_canary_moves_canary_to_stable() -> None:
    state = CanaryState(
        stable_version="old",
        canary_version="new",
        canary_percentage=25,
        canary_task_count=10,
        canary_pass_count=10,
    )
    promoted = promote_canary(state)
    assert promoted.stable_version == "new"
    assert promoted.canary_version == ""
    assert promoted.canary_percentage == 0
    assert promoted.canary_task_count == 0
    assert promoted.stable_task_count == 0


def test_promote_canary_preserves_thresholds() -> None:
    state = CanaryState(
        stable_version="old",
        canary_version="new",
        auto_promote_threshold=42,
        auto_rollback_diff_pct=7.5,
    )
    promoted = promote_canary(state)
    assert promoted.auto_promote_threshold == 42
    assert promoted.auto_rollback_diff_pct == pytest.approx(7.5)


def test_rollback_canary_clears_canary() -> None:
    state = CanaryState(
        stable_version="keep",
        canary_version="bad",
        canary_percentage=50,
        canary_task_count=10,
        canary_pass_count=2,
    )
    rolled = rollback_canary(state)
    assert rolled.stable_version == "keep"
    assert rolled.canary_version == ""
    assert rolled.canary_percentage == 0
    assert rolled.canary_task_count == 0
    assert rolled.canary_pass_count == 0


def test_rollback_canary_preserves_thresholds() -> None:
    state = CanaryState(
        stable_version="keep",
        canary_version="bad",
        auto_promote_threshold=20,
        auto_rollback_diff_pct=5.0,
    )
    rolled = rollback_canary(state)
    assert rolled.auto_promote_threshold == 20
    assert rolled.auto_rollback_diff_pct == pytest.approx(5.0)


# ---------------------------------------------------------------------------
# End-to-end scenario
# ---------------------------------------------------------------------------


def test_end_to_end_canary_lifecycle(tmp_path: Path) -> None:
    v1 = create_prompt_version("backend", "old prompt")
    v2 = create_prompt_version("backend", "new prompt")
    state = CanaryState(
        stable_version=v1.version_id,
        canary_version=v2.version_id,
        canary_percentage=20,
        auto_promote_threshold=5,
    )
    save_canary_state(state, tmp_path)
    reloaded = load_canary_state(tmp_path)
    assert reloaded is not None
    assert reloaded.canary_version == v2.version_id

    # Simulate 20 tasks; 20% route to canary.
    for i in range(20):
        routed = should_route_to_canary(reloaded, i)
        record_result(reloaded, used_canary=routed, passed=True)
    assert reloaded.canary_task_count + reloaded.stable_task_count == 20

    decision = evaluate_canary(reloaded)
    assert decision == "promote"
    promoted = promote_canary(reloaded)
    assert promoted.stable_version == v2.version_id


if __name__ == "__main__":
    pytest.main([__file__, "-x", "-q"])
