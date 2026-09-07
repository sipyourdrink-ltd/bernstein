"""Tests for harness fingerprinting, bundle comparison drift enforcement, and leaderboard grouping.

Refs: #5568.
"""

from __future__ import annotations

import pytest

from bernstein.eval.bench.bundle import SubmissionBundle, TaskResult
from bernstein.eval.bench.compare import HarnessDriftError, compare_bundles
from bernstein.eval.bench.fingerprint import compute_harness_fingerprint
from bernstein.eval.bench.leaderboard import Leaderboard, LeaderboardEntry


def _sample_task_result(task_id: str = "task-1", passed: bool = True, score: float = 1.0) -> TaskResult:
    return TaskResult(
        task_id=task_id,
        task_hash="abc123hash",
        receipt={"journal_head": "jh1", "spine_head": "sh1"},
        passed=passed,
        score=score,
    )


def _sample_bundle(
    harness_settings: dict | None = None,
    overall_score: float = 1.0,
    suite_hash: str = "suite-hash-1",
) -> SubmissionBundle:
    results = [_sample_task_result(passed=(overall_score > 0.5), score=overall_score)]
    return SubmissionBundle(
        suite_hash=suite_hash,
        suite_version="golden-v1",
        task_results=results,
        scheduler_config={"scheduler": "default"},
        harness_settings=harness_settings or {},
    )


def test_fingerprint_changes_when_a_prompt_template_changes() -> None:
    settings_v1 = {
        "effort": "high",
        "prompt_templates": {"system": "hash_version_1", "role": "hash_role_1"},
        "tool_allowlist": ["bash", "file_edit"],
    }
    settings_v2 = {
        "effort": "high",
        "prompt_templates": {"system": "hash_version_2", "role": "hash_role_1"},
        "tool_allowlist": ["bash", "file_edit"],
    }
    fp1 = compute_harness_fingerprint(settings_v1)
    fp2 = compute_harness_fingerprint(settings_v2)
    assert fp1 != fp2, "Harness fingerprint must change when a prompt template changes"


def test_fingerprint_is_stable_across_identical_runs() -> None:
    settings = {
        "effort": "high",
        "prompt_templates": {"system": "hash_version_1", "role": "hash_role_1"},
        "tool_allowlist": ["file_edit", "bash"],  # ordering difference in allowlist
        "decomposition": {"depth": 2},
    }
    settings_reordered = {
        "decomposition": {"depth": 2},
        "tool_allowlist": ["bash", "file_edit"],
        "prompt_templates": {"role": "hash_role_1", "system": "hash_version_1"},
        "effort": "high",
    }
    fp1 = compute_harness_fingerprint(settings)
    fp2 = compute_harness_fingerprint(settings_reordered)
    assert fp1 == fp2, "Harness fingerprint must be deterministic and stable across identical runs"


def test_compare_refuses_different_fingerprints_without_the_flag() -> None:
    bundle_a = _sample_bundle(
        harness_settings={"effort": "low", "sandbox": "none"},
        overall_score=0.8,
    )
    bundle_b = _sample_bundle(
        harness_settings={"effort": "high", "sandbox": "docker"},
        overall_score=0.9,
    )

    assert bundle_a.harness_fingerprint != bundle_b.harness_fingerprint

    # Without flag: raises HarnessDriftError
    with pytest.raises(HarnessDriftError) as exc_info:
        compare_bundles(bundle_a, bundle_b, allow_harness_drift=False)
    assert "Cannot compare bundles with different harness fingerprints" in str(exc_info.value)
    assert "effort" in str(exc_info.value)
    assert "sandbox" in str(exc_info.value)

    # With flag: permits ranking and reports deltas
    res = compare_bundles(bundle_a, bundle_b, allow_harness_drift=True)
    assert res.allowed_drift is True
    assert res.score_delta == pytest.approx(0.1)


def test_leaderboard_groups_rows_by_fingerprint() -> None:
    fp_a = "aaaaaaaa" * 8
    fp_b = "bbbbbbbb" * 8

    entry_1 = LeaderboardEntry(
        bundle_hash="hash-1",
        suite_hash="suite-1",
        suite_version="v1",
        overall_score=0.95,
        pass_rate=0.95,
        num_tasks=10,
        submitted_at=100.0,
        harness_fingerprint=fp_a,
    )
    entry_2 = LeaderboardEntry(
        bundle_hash="hash-2",
        suite_hash="suite-1",
        suite_version="v1",
        overall_score=0.90,
        pass_rate=0.90,
        num_tasks=10,
        submitted_at=110.0,
        harness_fingerprint=fp_b,
    )
    entry_3 = LeaderboardEntry(
        bundle_hash="hash-3",
        suite_hash="suite-1",
        suite_version="v1",
        overall_score=0.85,
        pass_rate=0.85,
        num_tasks=10,
        submitted_at=120.0,
        harness_fingerprint=fp_a,
    )

    board = Leaderboard(suite_hash="suite-1", suite_version="v1")
    board.add_entry(entry_1)
    board.add_entry(entry_2)
    board.add_entry(entry_3)

    groups = board.groups_by_fingerprint()
    assert fp_a in groups
    assert fp_b in groups
    assert len(groups[fp_a]) == 2
    assert len(groups[fp_b]) == 1
    assert groups[fp_a][0].bundle_hash == "hash-1"
    assert groups[fp_a][1].bundle_hash == "hash-3"
