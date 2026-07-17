"""Unit tests for the drift-based freshness verdict (issue #2551, AC1).

The verdict is a pure function of ``(entry, policy, repo_state)``: two processes
given the same entry and repo state produce the byte-identical verdict JSON, and
the function reads no clock, no filesystem outside the injected worktree, and no
network.
"""

from __future__ import annotations

import builtins
import subprocess

import pytest

from bernstein.core.persistence.cache_policy import (
    REASON_BASE_NOT_ANCESTOR,
    REASON_BASE_OUTSIDE_WINDOW,
    REASON_FILE_DELETED,
    REASON_FILE_DRIFT,
    REASON_FRESH,
    REASON_TTL_EXPIRED,
    CacheEntry,
    CachePolicy,
    RepoState,
    evaluate_freshness,
)


def _entry(**over: object) -> CacheEntry:
    base = {
        "key": "deadbeef",
        "input_hashes": {"task_inputs": "sha256:in"},
        "output_hash": "sha256:out",
        "producing_task": "t-1",
        "diff_file_hashes": {"src/a.py": "sha256:aaa", "src/b.py": "sha256:bbb"},
        "base_commit": "c" * 40,
        "verified": True,
        "recipe_hash": "sha256:recipe",
        "policy_hash": "sha256:policy",
        "created_ts": 1000,
    }
    base.update(over)
    return CacheEntry(**base)  # type: ignore[arg-type]


def test_fresh_when_files_unchanged_and_base_is_head() -> None:
    policy = CachePolicy(expiry_mode="drift", drift_window=0)
    state = RepoState(
        file_hashes={"src/a.py": "sha256:aaa", "src/b.py": "sha256:bbb"},
        ancestor_distance=0,
    )
    verdict = evaluate_freshness(_entry(), policy, state)
    assert verdict.fresh is True
    assert verdict.reason == REASON_FRESH


def test_stale_when_a_touched_file_changed() -> None:
    policy = CachePolicy(expiry_mode="drift", drift_window=5)
    state = RepoState(
        file_hashes={"src/a.py": "sha256:CHANGED", "src/b.py": "sha256:bbb"},
        ancestor_distance=0,
    )
    verdict = evaluate_freshness(_entry(), policy, state)
    assert verdict.fresh is False
    assert verdict.reason == REASON_FILE_DRIFT
    assert verdict.detail == "src/a.py"


def test_stale_when_a_touched_file_deleted() -> None:
    policy = CachePolicy(expiry_mode="drift", drift_window=5)
    state = RepoState(file_hashes={"src/b.py": "sha256:bbb"}, ancestor_distance=0)
    verdict = evaluate_freshness(_entry(), policy, state)
    assert verdict.fresh is False
    assert verdict.reason == REASON_FILE_DELETED
    assert verdict.detail == "src/a.py"


def test_stale_when_base_not_ancestor() -> None:
    policy = CachePolicy(expiry_mode="drift", drift_window=5)
    state = RepoState(
        file_hashes={"src/a.py": "sha256:aaa", "src/b.py": "sha256:bbb"},
        ancestor_distance=None,
    )
    verdict = evaluate_freshness(_entry(), policy, state)
    assert verdict.fresh is False
    assert verdict.reason == REASON_BASE_NOT_ANCESTOR


def test_stale_when_base_outside_drift_window() -> None:
    policy = CachePolicy(expiry_mode="drift", drift_window=2)
    state = RepoState(
        file_hashes={"src/a.py": "sha256:aaa", "src/b.py": "sha256:bbb"},
        ancestor_distance=5,
    )
    verdict = evaluate_freshness(_entry(), policy, state)
    assert verdict.fresh is False
    assert verdict.reason == REASON_BASE_OUTSIDE_WINDOW
    assert verdict.detail == "5"


def test_within_window_is_fresh() -> None:
    policy = CachePolicy(expiry_mode="drift", drift_window=3)
    state = RepoState(
        file_hashes={"src/a.py": "sha256:aaa", "src/b.py": "sha256:bbb"},
        ancestor_distance=3,
    )
    assert evaluate_freshness(_entry(), policy, state).fresh is True


def test_drift_beats_ttl_ordering() -> None:
    # A stale file must be reported as file_drift even if TTL would also fire.
    policy = CachePolicy(expiry_mode="both", drift_window=5, ttl_seconds=10, world_facing=True)
    state = RepoState(
        file_hashes={"src/a.py": "sha256:CHANGED", "src/b.py": "sha256:bbb"},
        ancestor_distance=0,
        now=1_000_000,
    )
    verdict = evaluate_freshness(_entry(), policy, state)
    assert verdict.reason == REASON_FILE_DRIFT


def test_ttl_backstop_only_for_world_facing() -> None:
    fresh_files = {"src/a.py": "sha256:aaa", "src/b.py": "sha256:bbb"}
    # world_facing + both: TTL fires when expired.
    world = CachePolicy(expiry_mode="both", drift_window=5, ttl_seconds=10, world_facing=True)
    state = RepoState(file_hashes=fresh_files, ancestor_distance=0, now=2000)
    verdict = evaluate_freshness(_entry(created_ts=1000), world, state)
    assert verdict.fresh is False
    assert verdict.reason == REASON_TTL_EXPIRED

    # Non world-facing: TTL branch is inert even with the same timestamps.
    local = CachePolicy(expiry_mode="both", drift_window=5, ttl_seconds=10, world_facing=False)
    assert evaluate_freshness(_entry(created_ts=1000), local, state).fresh is True


def test_ttl_inert_without_injected_now() -> None:
    policy = CachePolicy(expiry_mode="both", drift_window=5, ttl_seconds=10, world_facing=True)
    state = RepoState(
        file_hashes={"src/a.py": "sha256:aaa", "src/b.py": "sha256:bbb"},
        ancestor_distance=0,
        now=None,
    )
    # No injected clock -> the verdict never reads the wall clock -> fresh.
    assert evaluate_freshness(_entry(created_ts=1000), policy, state).fresh is True


def test_verdict_json_is_byte_identical_across_processes() -> None:
    # AC1: two independent evaluations of the same (entry, repo state) produce
    # byte-identical verdict JSON.
    policy = CachePolicy(expiry_mode="drift", drift_window=1)
    state = RepoState(
        file_hashes={"src/a.py": "sha256:CHANGED", "src/b.py": "sha256:bbb"},
        ancestor_distance=0,
    )
    a = evaluate_freshness(_entry(), policy, state).canonical_json()
    b = evaluate_freshness(_entry(), policy, state).canonical_json()
    assert a == b
    assert isinstance(a, bytes)


def test_verdict_reads_no_clock_or_filesystem(monkeypatch: pytest.MonkeyPatch) -> None:
    # AC1 (enforced by test): the verdict function must not touch the wall
    # clock, the filesystem, or the network. Poison those and confirm the
    # verdict still computes from the injected repo state alone.
    import time as _time

    def _boom(*_a: object, **_k: object) -> float:
        raise AssertionError("verdict must not read the wall clock")

    def _boom_open(*_a: object, **_k: object) -> object:
        raise AssertionError("verdict must not touch the filesystem")

    def _boom_proc(*_a: object, **_k: object) -> object:
        raise AssertionError("verdict must not shell out / hit the network")

    monkeypatch.setattr(_time, "time", _boom)
    monkeypatch.setattr(builtins, "open", _boom_open)
    monkeypatch.setattr(subprocess, "run", _boom_proc)

    policy = CachePolicy(expiry_mode="both", drift_window=2, ttl_seconds=10, world_facing=True)
    state = RepoState(
        file_hashes={"src/a.py": "sha256:aaa", "src/b.py": "sha256:bbb"},
        ancestor_distance=1,
        now=1005,  # within the injected TTL relative to created_ts=1000
    )
    verdict = evaluate_freshness(_entry(created_ts=1000), policy, state)
    assert verdict.fresh is True
