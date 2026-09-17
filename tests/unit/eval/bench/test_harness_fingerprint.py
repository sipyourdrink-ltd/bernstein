"""
Harness-fingerprint tests for bernstein-bench (issue #5568).

The four acceptance tests named in the issue, plus the boundary cases
recorded in the patch matrix: save/load round trip, pre-#5568 legacy
bundle load, stored-fingerprint integrity recompute, same-fingerprint
ranking, and leaderboard round trip.  All hermetic (MockReplayAdapter).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bernstein.eval.bench.bench_cli import bench_group
from bernstein.eval.bench.bundle import SubmissionBundle, harness_fingerprint
from bernstein.eval.bench.leaderboard import Leaderboard, LeaderboardEntry
from bernstein.eval.bench.runner import BenchRunner, MockReplayAdapter
from bernstein.eval.bench.suite import BenchSuite, BenchTask

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

#: Harness settings shaped like a real deployment's scheduler_config: the
#: keys the issue enumerates (decomposition, prompt template, tool
#: allowlist, effort/thinking budget, retry policy, timeouts, sandbox).
BASE_CFG: dict = {
    "scheduler": "default",
    "decomposition": {"depth": 2},
    "prompt_template": "code-review-v1",
    "tool_allowlist": ["read", "grep", "apply_patch"],
    "effort": "high",
    "thinking_budget": 4096,
    "retry": {"max_attempts": 2, "backoff_s": 5},
    "timeout_s": 900,
    "sandbox": "seatbelt",
}


@pytest.fixture()
def simple_suite() -> BenchSuite:
    """A minimal two-task suite for fast tests."""
    return BenchSuite(
        version="test-v1",
        tasks=[
            BenchTask(
                id="task_a",
                description="Task A",
                steps=("step 1", "step 2"),
                assertions=({"kind": "exists"},),
                category="cat1",
            ),
            BenchTask(
                id="task_b",
                description="Task B",
                steps=("step 1",),
                assertions=({"kind": "syntax_valid"},),
                category="cat2",
            ),
        ],
    )


@pytest.fixture()
def adapter() -> MockReplayAdapter:
    return MockReplayAdapter()


def _make_bundle(suite: BenchSuite, adapter: MockReplayAdapter, cfg: dict | None = None) -> SubmissionBundle:
    runner = BenchRunner(suite=suite, adapter=adapter, scheduler_config=cfg if cfg is not None else {})
    return runner.run()


def _write_bundle(bundle: SubmissionBundle, directory: Path, name: str) -> Path:
    path = directory / name
    bundle.save(path)
    return path


# ===========================================================================
# AC-1 / AC-2 — the fingerprint itself
# ===========================================================================


class TestHarnessFingerprint:
    def test_fingerprint_changes_when_a_prompt_template_changes(
        self, simple_suite: BenchSuite, adapter: MockReplayAdapter
    ) -> None:
        cfg_a = {**BASE_CFG, "prompt_template": "code-review-v1"}
        cfg_b = {**BASE_CFG, "prompt_template": "code-review-v2"}
        bundle_a = _make_bundle(simple_suite, adapter, cfg_a)
        bundle_b = _make_bundle(simple_suite, adapter, cfg_b)

        assert bundle_a.harness_fingerprint != ""
        assert bundle_a.harness_fingerprint != bundle_b.harness_fingerprint

    def test_fingerprint_is_stable_across_identical_runs(
        self, simple_suite: BenchSuite, adapter: MockReplayAdapter, tmp_path: Path
    ) -> None:
        # Two separately built config dicts with shuffled key order: the
        # canonical JSON must make insertion order irrelevant.
        cfg_1 = {k: BASE_CFG[k] for k in reversed(list(BASE_CFG))}
        cfg_2 = dict(BASE_CFG)
        bundle_1 = _make_bundle(simple_suite, adapter, cfg_1)
        bundle_2 = _make_bundle(simple_suite, adapter, cfg_2)

        assert bundle_1.harness_fingerprint == bundle_2.harness_fingerprint

        # The stored value is exactly the canonical function of the raw
        # settings carried beside it.
        assert bundle_1.harness_fingerprint == harness_fingerprint(bundle_1.scheduler_config)

        # A save/load round trip preserves it byte-for-byte.
        path = _write_bundle(bundle_1, tmp_path, "bundle.json")
        loaded = SubmissionBundle.load(path)
        assert loaded.harness_fingerprint == bundle_1.harness_fingerprint

    def test_legacy_bundle_without_the_field_still_loads(
        self, simple_suite: BenchSuite, adapter: MockReplayAdapter, tmp_path: Path
    ) -> None:
        bundle = _make_bundle(simple_suite, adapter, BASE_CFG)
        path = _write_bundle(bundle, tmp_path, "legacy.json")
        raw = json.loads(path.read_text(encoding="utf-8"))
        del raw["harness_fingerprint"]
        path.write_text(json.dumps(raw), encoding="utf-8")

        loaded = SubmissionBundle.load(path)
        # The fingerprint is a pure function of scheduler_config, so a
        # pre-#5568 bundle derives it on load instead of failing.
        assert loaded.harness_fingerprint == harness_fingerprint(loaded.scheduler_config)


# ===========================================================================
# AC-3 — bench compare
# ===========================================================================


class TestBenchCompare:
    def test_compare_refuses_different_fingerprints_without_the_flag(
        self, simple_suite: BenchSuite, adapter: MockReplayAdapter, tmp_path: Path
    ) -> None:
        from click.testing import CliRunner

        bundle_a = _make_bundle(simple_suite, adapter, {**BASE_CFG, "prompt_template": "code-review-v1"})
        bundle_b = _make_bundle(simple_suite, adapter, {**BASE_CFG, "prompt_template": "code-review-v2"})
        path_a = _write_bundle(bundle_a, tmp_path, "a.json")
        path_b = _write_bundle(bundle_b, tmp_path, "b.json")

        result = CliRunner().invoke(bench_group, ["compare", str(path_a), str(path_b)])
        assert result.exit_code == 1
        assert "prompt_template" in result.output
        assert "--allow-harness-drift" in result.output

        overridden = CliRunner().invoke(bench_group, ["compare", str(path_a), str(path_b), "--allow-harness-drift"])
        assert overridden.exit_code == 0, overridden.output
        assert "prompt_template" in overridden.output

    def test_compare_ranks_same_fingerprint_bundles(
        self, simple_suite: BenchSuite, adapter: MockReplayAdapter, tmp_path: Path
    ) -> None:
        from click.testing import CliRunner

        bundle_a = _make_bundle(simple_suite, adapter, BASE_CFG)
        bundle_b = _make_bundle(simple_suite, adapter, dict(BASE_CFG))
        path_a = _write_bundle(bundle_a, tmp_path, "a.json")
        path_b = _write_bundle(bundle_b, tmp_path, "b.json")

        result = CliRunner().invoke(bench_group, ["compare", str(path_a), str(path_b)])
        assert result.exit_code == 0, result.output
        assert path_a.name in result.output
        assert path_b.name in result.output
        assert bundle_a.harness_fingerprint in result.output

    def test_compare_refuses_a_stored_fingerprint_that_does_not_match_its_settings(
        self, simple_suite: BenchSuite, adapter: MockReplayAdapter, tmp_path: Path
    ) -> None:
        from click.testing import CliRunner

        bundle_a = _make_bundle(simple_suite, adapter, BASE_CFG)
        bundle_b = _make_bundle(simple_suite, adapter, BASE_CFG)
        path_a = _write_bundle(bundle_a, tmp_path, "a.json")
        path_b = _write_bundle(bundle_b, tmp_path, "b.json")

        # Edit the stored fingerprint without touching scheduler_config.
        # It sits outside bundle_hash (a projection of settings already
        # hashed), so load succeeds and compare's recompute must catch it.
        raw = json.loads(path_a.read_text(encoding="utf-8"))
        raw["harness_fingerprint"] = "f4" * 32
        path_a.write_text(json.dumps(raw), encoding="utf-8")

        result = CliRunner().invoke(bench_group, ["compare", str(path_a), str(path_b), "--allow-harness-drift"])
        assert result.exit_code == 1
        assert "integrity" in result.output


# ===========================================================================
# AC-4 — leaderboard groups rows by fingerprint
# ===========================================================================


class TestLeaderboardGrouping:
    def test_leaderboard_groups_rows_by_fingerprint(self) -> None:
        fp_1 = "1f" * 32
        fp_2 = "2e" * 32

        def entry(fp: str, score: float) -> LeaderboardEntry:
            return LeaderboardEntry(
                bundle_hash=f"hash-{fp[:4]}-{score}",
                suite_hash="x",
                suite_version="v1",
                overall_score=score,
                pass_rate=score,
                num_tasks=2,
                submitted_at=1.0,
                harness_fingerprint=fp,
            )

        lb = Leaderboard(suite_hash="x", suite_version="v1")
        # The fp_2 row outscores both fp_1 rows globally; grouping must
        # still rank it inside its own section, not at the top of fp_1's.
        lb.add_entry(entry(fp_1, 0.9))
        lb.add_entry(entry(fp_1, 0.8))
        lb.add_entry(entry(fp_2, 1.0))

        md = lb.to_markdown()

        sections = [chunk for chunk in md.split("### Harness ")[1:]]
        assert len(sections) == 2
        # Every row shows its fingerprint prefix.
        assert fp_1[:12] in md
        assert fp_2[:12] in md
        # Ranks restart within each group (data rows only — the first
        # two pipe lines per section are the header and its rule).
        # The fp_2 group has the global best score, so it renders first.
        first, second = sections
        first_ranks = [line.split("|")[1].strip() for line in first.splitlines() if line.startswith("|")][2:]
        second_ranks = [line.split("|")[1].strip() for line in second.splitlines() if line.startswith("|")][2:]
        assert first_ranks == ["1"]
        assert second_ranks == ["1", "2"]

    def test_leaderboard_round_trip_preserves_fingerprint(self, tmp_path: Path) -> None:
        lb = Leaderboard(suite_hash="x", suite_version="v1")
        lb.add_entry(
            LeaderboardEntry(
                bundle_hash="hash-x",
                suite_hash="x",
                suite_version="v1",
                overall_score=1.0,
                pass_rate=1.0,
                num_tasks=2,
                submitted_at=1.0,
                harness_fingerprint="ab" * 32,
            )
        )
        path = tmp_path / "lb.json"
        lb.save(path)
        loaded = Leaderboard.load(path)
        assert loaded.entries[0].harness_fingerprint == "ab" * 32


# ===========================================================================
# AC-5 — documented schema and key coverage
# ===========================================================================


class TestDocs:
    def test_docs_cover_the_fingerprint_and_compare(self) -> None:
        repo_root = Path(__file__).parents[4]  # tests/unit/eval/bench/test_harness_fingerprint.py -> repo root
        docs_path = repo_root / "docs" / "eval" / "bench.md"
        assert docs_path.exists(), f"docs/eval/bench.md not found at {docs_path}."
        content = docs_path.read_text(encoding="utf-8")
        assert "harness_fingerprint" in content
        assert "bernstein bench compare" in content
        assert "--allow-harness-drift" in content
