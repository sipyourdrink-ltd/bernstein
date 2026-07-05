"""Three-arm profile A/B comparison artifact (issue #2247).

Acceptance criteria under test:

1. The comparison artifact over the same recorded run set re-serialises
   byte-identically; suite and profile hashes pin exactly what was
   compared.
2. Every per-arm cost figure resolves to concrete ledger entries by
   reference; a verifier can recompute the aggregates from the ledger
   alone.
3. The full three-arm flow runs with the synthetic executor: zero
   network, deterministic verdicts, real ledger rows.
4. A winner declaration requires both cost and quality measured on both
   arms; a run missing either emits ``incomparable``, never a partial
   winner.
"""

from __future__ import annotations

import hashlib
import json
import statistics
from pathlib import Path

import pytest

from bernstein.core.agents.response_style import addendum_sha256, render_style_addendum
from bernstein.core.cost.spend_ledger import SpendLedger
from bernstein.core.security.audit_chain import (
    EVENT_EVAL_AB_COMPARISON,
    AuditChainStore,
    record_eval_ab_comparison,
)
from bernstein.eval.ab_comparison import (
    ARM_BASELINE,
    ARM_CANDIDATE,
    ARM_CONTROL,
    ARTIFACT_KIND,
    ARTIFACT_VERSION,
    CONTROL_ADDENDUM,
    INCOMPARABLE,
    NOT_MEASURED,
    ArmRunRow,
    append_comparison_index,
    build_arms,
    build_comparison_artifact,
    latest_comparison_for_pair,
    run_arms,
    suite_file_sha256,
    synthetic_arm_executor,
    write_comparison_artifact,
)
from bernstein.eval.ab_runner import Task

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _tasks() -> list[Task]:
    """Tasks whose expected outputs make the candidate arm pass 2/2."""
    return [
        Task(task_id="t1", input="hello", expected="candidate::hello"),
        Task(task_id="t2", input="world", expected="candidate::world"),
    ]


@pytest.fixture()
def ledger_path(tmp_path: Path) -> Path:
    return tmp_path / "cost" / "ledger.jsonl"


def _run_three_arm(ledger_path: Path, tasks: list[Task] | None = None, *, trials: int = 2):
    """Run the canonical three-arm synthetic flow and build the artifact."""
    plan = build_arms("balanced", "terse", arms=3)
    ledger = SpendLedger(path=ledger_path, run_id="eval-ab-test")
    executor = synthetic_arm_executor(ledger, run_token="tok")
    rows = run_arms(plan, tasks if tasks is not None else _tasks(), executor=executor, trials=trials)
    artifact = build_comparison_artifact(
        plan=plan,
        rows=rows,
        ledger_path=ledger_path,
        suite_sha256="ab" * 32,
        suite_name="suite.yaml",
        adapter_versions={"bernstein": "0.0.0"},
        trials=trials,
    )
    return plan, rows, artifact


# ---------------------------------------------------------------------------
# Arm plans
# ---------------------------------------------------------------------------


class TestBuildArms:
    def test_two_arm_plan_uses_profile_names(self) -> None:
        plan = build_arms("balanced", "terse")
        assert [a.name for a in plan.arms] == ["balanced", "terse"]
        assert plan.honest_pair == ("balanced", "terse")
        assert plan.conflated_pair is None
        assert plan.profile_pair == ("balanced", "terse")

    def test_two_arm_addendum_hashes_pin_rendered_content(self) -> None:
        plan = build_arms("balanced", "terse")
        assert plan.arms[0].addendum_sha256 == addendum_sha256("")
        assert plan.arms[1].addendum_sha256 == addendum_sha256(render_style_addendum("terse"))

    def test_two_arm_rejects_same_profile(self) -> None:
        with pytest.raises(ValueError, match="differ"):
            build_arms("terse", "terse")

    def test_two_arm_rejects_unknown_profile(self) -> None:
        with pytest.raises(ValueError, match="unknown response style"):
            build_arms("balanced", "bogus")

    def test_three_arm_plan_has_baseline_control_candidate(self) -> None:
        plan = build_arms("balanced", "terse", arms=3)
        assert [a.name for a in plan.arms] == [ARM_BASELINE, ARM_CONTROL, ARM_CANDIDATE]
        baseline, control, candidate = plan.arms
        assert baseline.profile == ""
        assert baseline.addendum_sha256 == addendum_sha256("")
        assert control.profile == ""
        assert control.addendum_sha256 == addendum_sha256(CONTROL_ADDENDUM)
        assert candidate.profile == "terse"
        assert candidate.addendum_sha256 == addendum_sha256(render_style_addendum("terse"))

    def test_three_arm_honest_pair_is_control_vs_candidate(self) -> None:
        plan = build_arms("baseline", "verbose", arms=3)
        assert plan.honest_pair == (ARM_CONTROL, ARM_CANDIDATE)
        assert plan.conflated_pair == (ARM_BASELINE, ARM_CANDIDATE)

    def test_three_arm_requires_unset_baseline_arm_a(self) -> None:
        with pytest.raises(ValueError, match="baseline"):
            build_arms("terse", "verbose", arms=3)

    def test_three_arm_rejects_balanced_candidate(self) -> None:
        # ``balanced`` renders an empty addendum; as a candidate it would
        # be indistinguishable from the baseline arm.
        with pytest.raises(ValueError, match="candidate"):
            build_arms("balanced", "balanced", arms=3)

    def test_invalid_arm_count_rejected(self) -> None:
        with pytest.raises(ValueError, match="arms"):
            build_arms("balanced", "terse", arms=4)


# ---------------------------------------------------------------------------
# Synthetic executor
# ---------------------------------------------------------------------------


class TestSyntheticExecutor:
    def test_deterministic_rows_and_ledger_entries(self, ledger_path: Path) -> None:
        plan = build_arms("balanced", "terse", arms=3)
        ledger = SpendLedger(path=ledger_path, run_id="r-1")
        executor = synthetic_arm_executor(ledger, run_token="tok")
        rows = run_arms(plan, _tasks(), executor=executor, trials=1)

        # 3 arms x 2 tasks x 1 trial.
        assert len(rows) == 6
        entries = SpendLedger.load_entries(ledger_path)
        assert len(entries) == 6
        # One ledger entry per run, joined by ledger_task_id.
        assert {r.ledger_task_id for r in rows} == {e.task_id for e in entries}

    def test_candidate_entries_tagged_with_profile(self, ledger_path: Path) -> None:
        plan = build_arms("balanced", "terse", arms=3)
        ledger = SpendLedger(path=ledger_path, run_id="r-1")
        run_arms(plan, _tasks(), executor=synthetic_arm_executor(ledger, run_token="tok"), trials=1)

        entries = SpendLedger.load_entries(ledger_path)
        candidate = [e for e in entries if f":{ARM_CANDIDATE}:" in e.task_id]
        baseline = [e for e in entries if f":{ARM_BASELINE}:" in e.task_id]
        assert candidate and baseline
        assert all(e.tags.get("response_profile") == "terse" for e in candidate)
        assert all("response_profile" not in e.tags for e in baseline)

    def test_verdict_from_expected_output(self, ledger_path: Path) -> None:
        plan = build_arms("balanced", "terse", arms=3)
        ledger = SpendLedger(path=ledger_path, run_id="r-1")
        rows = run_arms(plan, _tasks(), executor=synthetic_arm_executor(ledger, run_token="tok"), trials=1)

        by_arm: dict[str, list[ArmRunRow]] = {}
        for row in rows:
            by_arm.setdefault(row.arm, []).append(row)
        assert all(r.verdict == "pass" for r in by_arm[ARM_CANDIDATE])
        assert all(r.verdict == "fail" for r in by_arm[ARM_BASELINE])
        assert all(r.verdict == "fail" for r in by_arm[ARM_CONTROL])

    def test_missing_expected_is_not_measured(self, ledger_path: Path) -> None:
        plan = build_arms("balanced", "terse", arms=3)
        ledger = SpendLedger(path=ledger_path, run_id="r-1")
        tasks = [Task(task_id="t1", input="hello")]
        rows = run_arms(plan, tasks, executor=synthetic_arm_executor(ledger, run_token="tok"), trials=1)
        assert all(r.verdict == "not_measured" for r in rows)


# ---------------------------------------------------------------------------
# AC1 - byte-identical artifact over the same recorded run set
# ---------------------------------------------------------------------------


class TestArtifactDeterminism:
    def test_rebuild_from_same_run_set_is_byte_identical(self, ledger_path: Path) -> None:
        plan, rows, artifact_1 = _run_three_arm(ledger_path)
        artifact_2 = build_comparison_artifact(
            plan=plan,
            rows=rows,
            ledger_path=ledger_path,
            suite_sha256="ab" * 32,
            suite_name="suite.yaml",
            adapter_versions={"bernstein": "0.0.0"},
            trials=2,
        )
        assert artifact_1.artifact_bytes() == artifact_2.artifact_bytes()
        assert artifact_1.sha256 == artifact_2.sha256

    def test_suite_and_profile_hashes_pin_the_comparison(self, ledger_path: Path) -> None:
        _plan, _rows, artifact = _run_three_arm(ledger_path)
        content = artifact.content
        assert content["kind"] == ARTIFACT_KIND
        assert content["version"] == ARTIFACT_VERSION
        assert content["suite_sha256"] == "ab" * 32
        # Honest pair hashes: control vs candidate.
        assert content["profile_a_sha256"] == addendum_sha256(CONTROL_ADDENDUM)
        assert content["profile_b_sha256"] == addendum_sha256(render_style_addendum("terse"))
        # Every arm's addendum hash is pinned in the arms block.
        assert set(content["arms"]) == {ARM_BASELINE, ARM_CONTROL, ARM_CANDIDATE}
        assert content["arms"][ARM_CANDIDATE]["profile"] == "terse"

    def test_no_timestamps_in_hashed_payload(self, ledger_path: Path) -> None:
        _plan, _rows, artifact = _run_three_arm(ledger_path)
        flat = json.dumps(artifact.content)
        for needle in ('"ts"', "ts_iso", "timestamp", "generated_at"):
            assert needle not in flat

    def test_sha256_matches_canonical_content(self, ledger_path: Path) -> None:
        from bernstein.core.cost.profile_report import canonical_json_bytes

        _plan, _rows, artifact = _run_three_arm(ledger_path)
        assert artifact.sha256 == hashlib.sha256(canonical_json_bytes(artifact.content)).hexdigest()

    def test_changed_suite_hash_changes_artifact_hash(self, ledger_path: Path) -> None:
        plan, rows, artifact = _run_three_arm(ledger_path)
        other = build_comparison_artifact(
            plan=plan,
            rows=rows,
            ledger_path=ledger_path,
            suite_sha256="cd" * 32,
            suite_name="suite.yaml",
            adapter_versions={"bernstein": "0.0.0"},
            trials=2,
        )
        assert other.sha256 != artifact.sha256


# ---------------------------------------------------------------------------
# AC2 - cost figures resolve to ledger entries; aggregates recomputable
# ---------------------------------------------------------------------------


def _lines_by_hash(ledger_path: Path) -> dict[str, dict[str, object]]:
    out: dict[str, dict[str, object]] = {}
    for line in ledger_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            out[hashlib.sha256(line.encode("utf-8")).hexdigest()] = json.loads(line)
    return out


class TestLedgerResolution:
    def test_every_row_cost_resolves_by_reference(self, ledger_path: Path) -> None:
        _plan, _rows, artifact = _run_three_arm(ledger_path)
        resolvable = _lines_by_hash(ledger_path)
        for row in artifact.content["per_task"]:
            assert row["ledger_ref"], f"row {row['task_id']}/{row['arm']} has no ledger reference"
            entries = [resolvable[ref] for ref in row["ledger_ref"]]
            tokens = sum(int(e["input_tokens"]) + int(e["output_tokens"]) for e in entries)
            usd = round(sum(float(e["cost_usd"]) for e in entries), 6)
            assert row["tokens"] == tokens
            assert row["usd"] == usd

    def test_aggregates_recomputable_from_ledger_alone(self, ledger_path: Path) -> None:
        _plan, _rows, artifact = _run_three_arm(ledger_path)
        resolvable = _lines_by_hash(ledger_path)
        per_arm_usd: dict[str, list[float]] = {}
        per_arm_tokens: dict[str, list[int]] = {}
        for row in artifact.content["per_task"]:
            entries = [resolvable[ref] for ref in row["ledger_ref"]]
            per_arm_usd.setdefault(row["arm"], []).append(round(sum(float(e["cost_usd"]) for e in entries), 6))
            per_arm_tokens.setdefault(row["arm"], []).append(
                sum(int(e["input_tokens"]) + int(e["output_tokens"]) for e in entries)
            )
        for arm, agg in artifact.content["aggregates"].items():
            assert agg["median_usd"] == round(statistics.median(per_arm_usd[arm]), 6)
            assert agg["median_tokens"] == statistics.median(per_arm_tokens[arm])
            assert agg["total_usd"] == round(sum(per_arm_usd[arm]), 6)
            assert agg["total_tokens"] == sum(per_arm_tokens[arm])

    def test_rows_without_ledger_entries_have_null_cost(self, ledger_path: Path) -> None:
        plan, rows, _artifact = _run_three_arm(ledger_path)
        empty_ledger = ledger_path.parent / "empty.jsonl"
        artifact = build_comparison_artifact(
            plan=plan,
            rows=rows,
            ledger_path=empty_ledger,
            suite_sha256="ab" * 32,
            suite_name="suite.yaml",
            adapter_versions={"bernstein": "0.0.0"},
            trials=2,
        )
        for row in artifact.content["per_task"]:
            assert row["ledger_ref"] == []
            assert row["tokens"] is None
            assert row["usd"] is None


# ---------------------------------------------------------------------------
# Winner logic (AC4) - explicit rows against a forged ledger
# ---------------------------------------------------------------------------


def _forged_ledger(path: Path, costs: dict[str, float]) -> None:
    """Write one deterministic ledger row per ledger_task_id."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for task_id, cost in costs.items():
            row = {
                "ts": 1000.0,
                "ts_iso": "2026-01-01T00:00:00+00:00",
                "run_id": "r-1",
                "task_id": task_id,
                "agent_id": "a-1",
                "role": "eval",
                "feature_label": "eval_ab",
                "model": "sonnet",
                "input_tokens": 50,
                "output_tokens": 100,
                "cache_read_tokens": 0,
                "cache_write_tokens": 0,
                "cost_usd": cost,
                "quota_envelope": "subscription",
                "tags": {},
            }
            fh.write(json.dumps(row, sort_keys=False, separators=(",", ":")) + "\n")


def _row(task_id: str, arm: str, verdict: str, ledger_task_id: str, *, score: float | None = None) -> ArmRunRow:
    if score is None:
        score = 1.0 if verdict == "pass" else 0.0
    return ArmRunRow(
        task_id=task_id,
        arm=arm,
        trial=0,
        verdict=verdict,
        score=score,
        ledger_task_id=ledger_task_id,
    )


def _pair_artifact(tmp_path: Path, rows: list[ArmRunRow], costs: dict[str, float]):
    ledger = tmp_path / "ledger.jsonl"
    _forged_ledger(ledger, costs)
    plan = build_arms("balanced", "terse")
    return build_comparison_artifact(
        plan=plan,
        rows=tuple(rows),
        ledger_path=ledger,
        suite_sha256="ab" * 32,
        suite_name="suite.yaml",
        adapter_versions={"bernstein": "0.0.0"},
        trials=1,
    )


class TestWinner:
    def test_quality_winner_when_both_measured(self, tmp_path: Path) -> None:
        rows = [
            _row("t1", "balanced", "fail", "l-a1"),
            _row("t1", "terse", "pass", "l-b1"),
            _row("t2", "balanced", "fail", "l-a2"),
            _row("t2", "terse", "pass", "l-b2"),
        ]
        costs = {"l-a1": 0.10, "l-a2": 0.10, "l-b1": 0.10, "l-b2": 0.10}
        artifact = _pair_artifact(tmp_path, rows, costs)
        winner = artifact.content["winner"]
        assert winner["arm"] == "terse"
        assert winner["missing"] == []
        assert "pass_rate" in winner["reason"]

    def test_cost_breaks_quality_tie(self, tmp_path: Path) -> None:
        rows = [
            _row("t1", "balanced", "pass", "l-a1"),
            _row("t1", "terse", "pass", "l-b1"),
        ]
        costs = {"l-a1": 0.30, "l-b1": 0.10}
        artifact = _pair_artifact(tmp_path, rows, costs)
        winner = artifact.content["winner"]
        assert winner["arm"] == "terse"
        assert "median_usd" in winner["reason"]

    def test_tie_when_quality_and_cost_equal(self, tmp_path: Path) -> None:
        rows = [
            _row("t1", "balanced", "pass", "l-a1"),
            _row("t1", "terse", "pass", "l-b1"),
        ]
        costs = {"l-a1": 0.10, "l-b1": 0.10}
        artifact = _pair_artifact(tmp_path, rows, costs)
        assert artifact.content["winner"]["arm"] == "tie"

    def test_missing_quality_emits_incomparable(self, tmp_path: Path) -> None:
        rows = [
            _row("t1", "balanced", "not_measured", "l-a1"),
            _row("t1", "terse", "pass", "l-b1"),
        ]
        costs = {"l-a1": 0.10, "l-b1": 0.10}
        artifact = _pair_artifact(tmp_path, rows, costs)
        winner = artifact.content["winner"]
        assert winner["arm"] == INCOMPARABLE
        assert "quality:balanced" in winner["missing"]

    def test_missing_cost_emits_incomparable_never_partial(self, tmp_path: Path) -> None:
        rows = [
            _row("t1", "balanced", "fail", "l-a1"),
            _row("t1", "terse", "pass", "l-b-unrecorded"),
        ]
        # Arm B's run produced no ledger entry: quality alone must not
        # declare a winner.
        costs = {"l-a1": 0.10}
        artifact = _pair_artifact(tmp_path, rows, costs)
        winner = artifact.content["winner"]
        assert winner["arm"] == INCOMPARABLE
        assert "cost:terse" in winner["missing"]

    def test_empty_run_set_is_incomparable(self, tmp_path: Path) -> None:
        artifact = _pair_artifact(tmp_path, [], {})
        assert artifact.content["winner"]["arm"] == INCOMPARABLE


# ---------------------------------------------------------------------------
# Deltas: honest vs conflated labelling
# ---------------------------------------------------------------------------


class TestDeltas:
    def test_three_arm_deltas_labelled(self, ledger_path: Path) -> None:
        _plan, _rows, artifact = _run_three_arm(ledger_path)
        deltas = artifact.content["deltas"]
        honest = deltas["candidate_vs_control"]
        conflated = deltas["candidate_vs_baseline"]
        assert honest["conflated"] is False
        assert honest["pair"] == [ARM_CONTROL, ARM_CANDIDATE]
        assert conflated["conflated"] is True
        assert conflated["pair"] == [ARM_BASELINE, ARM_CANDIDATE]

    def test_delta_values_derive_from_aggregates(self, ledger_path: Path) -> None:
        _plan, _rows, artifact = _run_three_arm(ledger_path)
        agg = artifact.content["aggregates"]
        honest = artifact.content["deltas"]["candidate_vs_control"]
        assert honest["pass_rate_delta"] == round(agg[ARM_CANDIDATE]["pass_rate"] - agg[ARM_CONTROL]["pass_rate"], 4)
        assert honest["median_usd_delta"] == round(agg[ARM_CANDIDATE]["median_usd"] - agg[ARM_CONTROL]["median_usd"], 6)

    def test_two_arm_has_single_honest_delta(self, tmp_path: Path) -> None:
        rows = [
            _row("t1", "balanced", "pass", "l-a1"),
            _row("t1", "terse", "pass", "l-b1"),
        ]
        artifact = _pair_artifact(tmp_path, rows, {"l-a1": 0.10, "l-b1": 0.10})
        deltas = artifact.content["deltas"]
        assert list(deltas) == ["terse_vs_balanced"]
        assert deltas["terse_vs_balanced"]["conflated"] is False


# ---------------------------------------------------------------------------
# Structured "not measured" block
# ---------------------------------------------------------------------------


class TestNotMeasured:
    def test_block_lists_stable_identifiers(self, ledger_path: Path) -> None:
        _plan, _rows, artifact = _run_three_arm(ledger_path)
        assert artifact.content["not_measured"] == list(NOT_MEASURED)
        assert artifact.content["not_measured"] == sorted(artifact.content["not_measured"])
        assert "latency" in artifact.content["not_measured"]
        assert "fidelity_beyond_suite_verdicts" in artifact.content["not_measured"]
        assert "cross_model_generalization" in artifact.content["not_measured"]


# ---------------------------------------------------------------------------
# Artifact IO, chain anchoring, and pair index
# ---------------------------------------------------------------------------


class TestArtifactAndChain:
    def test_artifact_is_content_addressed(self, tmp_path: Path, ledger_path: Path) -> None:
        _plan, _rows, artifact = _run_three_arm(ledger_path)
        out = write_comparison_artifact(artifact, tmp_path / "reports")
        assert out.name == f"{artifact.sha256}.json"
        envelope = json.loads(out.read_text(encoding="utf-8"))
        assert envelope["artifact_sha256"] == artifact.sha256
        assert envelope["content"] == artifact.content
        assert out.read_bytes() == artifact.artifact_bytes()

    def test_chain_event_carries_hashes_and_verifies(self, tmp_path: Path, ledger_path: Path) -> None:
        _plan, _rows, artifact = _run_three_arm(ledger_path)
        chain = AuditChainStore(tmp_path / "audit", key=b"k" * 32)
        event = record_eval_ab_comparison(
            chain=chain,
            artifact_sha256=artifact.sha256,
            suite_sha256=str(artifact.content["suite_sha256"]),
            profile_a_sha256=str(artifact.content["profile_a_sha256"]),
            profile_b_sha256=str(artifact.content["profile_b_sha256"]),
            arm_count=len(artifact.content["arms"]),
            row_count=len(artifact.content["per_task"]),
            winner_arm=str(artifact.content["winner"]["arm"]),
            artifact_name=artifact.artifact_name,
        )
        assert event.event_type == EVENT_EVAL_AB_COMPARISON
        assert event.details["artifact_sha256"] == artifact.sha256
        assert event.details["prev_chain_digest"] is not None
        ok, errors = chain.verify()
        assert ok, errors

    def test_index_returns_latest_for_pair(self, tmp_path: Path, ledger_path: Path) -> None:
        _plan, _rows, artifact = _run_three_arm(ledger_path)
        reports = tmp_path / "reports"
        append_comparison_index(reports, profile_pair=("balanced", "terse"), artifact=artifact, ts=100.0)
        _plan2, _rows2, artifact_2 = _run_three_arm(ledger_path, trials=1)
        append_comparison_index(reports, profile_pair=("terse", "balanced"), artifact=artifact_2, ts=200.0)

        found = latest_comparison_for_pair(reports, "balanced", "terse")
        assert found is not None
        assert found["artifact_sha256"] == artifact_2.sha256
        # Order-insensitive lookup.
        assert latest_comparison_for_pair(reports, "terse", "balanced") == found

    def test_index_missing_pair_returns_none(self, tmp_path: Path) -> None:
        assert latest_comparison_for_pair(tmp_path / "reports", "terse", "verbose") is None


# ---------------------------------------------------------------------------
# Suite hashing
# ---------------------------------------------------------------------------


class TestSuiteHash:
    def test_suite_file_sha256_is_file_content_hash(self, tmp_path: Path) -> None:
        suite = tmp_path / "suite.yaml"
        suite.write_text("tasks:\n  - id: t1\n    input: hello\n", encoding="utf-8")
        assert suite_file_sha256(suite) == hashlib.sha256(suite.read_bytes()).hexdigest()
