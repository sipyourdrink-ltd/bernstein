"""Unit tests for bernstein.eval.scenario_runner."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from bernstein.eval.scenario_runner import (
    Scenario,
    ScenarioBatchResult,
    ScenarioLimits,
    ScenarioRunner,
    ScenarioRunResult,
    ScenarioSetup,
    ScenarioSignal,
    ScenarioTask,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _minimal_yaml(
    *,
    scenario_id: str = "test-scenario",
    tier: str = "smoke",
    signals: list[dict] | None = None,
    setup_command: str | None = None,
) -> str:
    """Build a minimal scenario YAML string."""
    data: dict = {
        "id": scenario_id,
        "tier": tier,
        "task": {
            "title": "Test task",
            "role": "backend",
            "effort": "low",
            "model": "haiku",
        },
        "expected_signals": signals or [],
        "limits": {
            "max_cost_usd": 0.10,
            "max_duration_seconds": 60,
            "max_retries": 0,
        },
    }
    if setup_command is not None:
        data["setup"] = {"command": setup_command}
    return yaml.dump(data)


@pytest.fixture()
def scenarios_dir(tmp_path: Path) -> Path:
    """Temporary directory populated with a few scenario YAML files."""
    d = tmp_path / "scenarios"
    d.mkdir()
    (d / "01-smoke.yaml").write_text(
        _minimal_yaml(
            scenario_id="smoke-alpha",
            tier="smoke",
            signals=[{"type": "path_exists", "value": "README.md"}],
        )
    )
    (d / "02-standard.yaml").write_text(
        _minimal_yaml(
            scenario_id="standard-beta",
            tier="standard",
            signals=[{"type": "command_succeeds", "value": "echo ok"}],
        )
    )
    (d / "03-stretch.yaml").write_text(
        _minimal_yaml(
            scenario_id="stretch-gamma",
            tier="stretch",
            signals=[{"type": "command_succeeds", "value": "echo ok"}],
        )
    )
    return d


@pytest.fixture()
def runner(scenarios_dir: Path, tmp_path: Path) -> ScenarioRunner:
    """ScenarioRunner pointed at the temp scenarios dir and repo root."""
    return ScenarioRunner(scenarios_dir=scenarios_dir, repo_root=tmp_path)


# ---------------------------------------------------------------------------
# Parsing / loading
# ---------------------------------------------------------------------------


class TestScenarioLoading:
    def test_load_all_scenarios(self, runner: ScenarioRunner) -> None:
        scenarios = runner.load_scenarios()
        assert len(scenarios) == 3

    def test_load_by_tier(self, runner: ScenarioRunner) -> None:
        smokes = runner.load_scenarios(tier="smoke")
        assert len(smokes) == 1
        assert smokes[0].id == "smoke-alpha"
        assert smokes[0].tier == "smoke"

    def test_load_scenario_by_id(self, runner: ScenarioRunner) -> None:
        scenario = runner.load_scenario("standard-beta")
        assert scenario.id == "standard-beta"
        assert scenario.tier == "standard"

    def test_load_scenario_not_found_raises(self, runner: ScenarioRunner) -> None:
        with pytest.raises(FileNotFoundError):
            runner.load_scenario("nonexistent-id")

    def test_load_scenarios_missing_dir_raises(self, tmp_path: Path) -> None:
        bad_dir = tmp_path / "no_such_dir"
        r = ScenarioRunner(scenarios_dir=bad_dir, repo_root=tmp_path)
        with pytest.raises(FileNotFoundError):
            r.load_scenarios()

    def test_invalid_tier_raises(self, tmp_path: Path) -> None:
        d = tmp_path / "scenarios"
        d.mkdir()
        (d / "bad.yaml").write_text(_minimal_yaml(scenario_id="bad", tier="invalid-tier"))
        r = ScenarioRunner(scenarios_dir=d, repo_root=tmp_path)
        # Invalid file is skipped (warning logged), so result is empty
        scenarios = r.load_scenarios()
        assert len(scenarios) == 0

    def test_parsed_task_fields(self, runner: ScenarioRunner) -> None:
        scenario = runner.load_scenario("smoke-alpha")
        assert scenario.task.title == "Test task"
        assert scenario.task.role == "backend"
        assert scenario.task.effort == "low"
        assert scenario.task.model == "haiku"

    def test_parsed_limits(self, runner: ScenarioRunner) -> None:
        scenario = runner.load_scenario("smoke-alpha")
        assert scenario.limits.max_cost_usd == pytest.approx(0.10)
        assert scenario.limits.max_duration_seconds == 60
        assert scenario.limits.max_retries == 0

    def test_no_setup_command_defaults_to_none(self, runner: ScenarioRunner) -> None:
        scenario = runner.load_scenario("smoke-alpha")
        assert scenario.setup.command is None

    def test_setup_command_parsed(self, scenarios_dir: Path, tmp_path: Path) -> None:
        (scenarios_dir / "04-with-setup.yaml").write_text(
            _minimal_yaml(
                scenario_id="with-setup",
                tier="smoke",
                setup_command="echo hello",
            )
        )
        r = ScenarioRunner(scenarios_dir=scenarios_dir, repo_root=tmp_path)
        scenario = r.load_scenario("with-setup")
        assert scenario.setup.command == "echo hello"

    def test_sorted_by_filename(self, runner: ScenarioRunner) -> None:
        scenarios = runner.load_scenarios()
        ids = [s.id for s in scenarios]
        assert ids == ["smoke-alpha", "standard-beta", "stretch-gamma"]


# ---------------------------------------------------------------------------
# Setup execution
# ---------------------------------------------------------------------------


class TestSetupExecution:
    def test_no_command_returns_true(self, runner: ScenarioRunner, tmp_path: Path) -> None:
        scenario = Scenario(
            id="x",
            tier="smoke",
            setup=ScenarioSetup(command=None),
            task=ScenarioTask(title="t"),
            expected_signals=[],
            limits=ScenarioLimits(),
        )
        assert runner.run_setup(scenario) is True

    def test_successful_command_returns_true(self, tmp_path: Path, scenarios_dir: Path) -> None:
        r = ScenarioRunner(scenarios_dir=scenarios_dir, repo_root=tmp_path)
        scenario = Scenario(
            id="x",
            tier="smoke",
            setup=ScenarioSetup(command="echo hello"),
            task=ScenarioTask(title="t"),
            expected_signals=[],
            limits=ScenarioLimits(),
        )
        assert r.run_setup(scenario) is True

    def test_failing_command_returns_false(self, tmp_path: Path, scenarios_dir: Path) -> None:
        r = ScenarioRunner(scenarios_dir=scenarios_dir, repo_root=tmp_path)
        scenario = Scenario(
            id="x",
            tier="smoke",
            setup=ScenarioSetup(command="exit 1"),
            task=ScenarioTask(title="t"),
            expected_signals=[],
            limits=ScenarioLimits(),
        )
        assert r.run_setup(scenario) is False

    def test_setup_creates_file(self, tmp_path: Path, scenarios_dir: Path) -> None:
        r = ScenarioRunner(scenarios_dir=scenarios_dir, repo_root=tmp_path)
        target = tmp_path / "setup_output.txt"
        scenario = Scenario(
            id="x",
            tier="smoke",
            setup=ScenarioSetup(command=f"echo created > {target}"),
            task=ScenarioTask(title="t"),
            expected_signals=[],
            limits=ScenarioLimits(),
        )
        assert r.run_setup(scenario) is True
        assert target.exists()


# ---------------------------------------------------------------------------
# Signal evaluation
# ---------------------------------------------------------------------------


class TestSignalEvaluation:
    def _runner(self, tmp_path: Path, scenarios_dir: Path) -> ScenarioRunner:
        return ScenarioRunner(scenarios_dir=scenarios_dir, repo_root=tmp_path)

    def test_path_exists_true(self, tmp_path: Path, scenarios_dir: Path) -> None:
        (tmp_path / "myfile.txt").write_text("hi")
        r = self._runner(tmp_path, scenarios_dir)
        sig = ScenarioSignal(type="path_exists", value="myfile.txt")
        assert r.check_signal(sig) is True

    def test_path_exists_false(self, tmp_path: Path, scenarios_dir: Path) -> None:
        r = self._runner(tmp_path, scenarios_dir)
        sig = ScenarioSignal(type="path_exists", value="no_such_file.txt")
        assert r.check_signal(sig) is False

    def test_path_exists_uses_path_field(self, tmp_path: Path, scenarios_dir: Path) -> None:
        (tmp_path / "other.txt").write_text("hi")
        r = self._runner(tmp_path, scenarios_dir)
        sig = ScenarioSignal(type="path_exists", value="", path="other.txt")
        assert r.check_signal(sig) is True

    def test_file_contains_true(self, tmp_path: Path, scenarios_dir: Path) -> None:
        f = tmp_path / "sample.py"
        f.write_text("def hello(): pass\n")
        r = self._runner(tmp_path, scenarios_dir)
        sig = ScenarioSignal(type="file_contains", value="def hello", path="sample.py")
        assert r.check_signal(sig) is True

    def test_file_contains_false(self, tmp_path: Path, scenarios_dir: Path) -> None:
        f = tmp_path / "sample.py"
        f.write_text("def hello(): pass\n")
        r = self._runner(tmp_path, scenarios_dir)
        sig = ScenarioSignal(type="file_contains", value="def goodbye", path="sample.py")
        assert r.check_signal(sig) is False

    def test_file_contains_legacy_format(self, tmp_path: Path, scenarios_dir: Path) -> None:
        f = tmp_path / "code.py"
        f.write_text("# Args:\n")
        r = self._runner(tmp_path, scenarios_dir)
        sig = ScenarioSignal(type="file_contains", value="code.py :: Args:")
        assert r.check_signal(sig) is True

    def test_file_contains_missing_file(self, tmp_path: Path, scenarios_dir: Path) -> None:
        r = self._runner(tmp_path, scenarios_dir)
        sig = ScenarioSignal(type="file_contains", value="text", path="no_file.py")
        assert r.check_signal(sig) is False

    def test_command_succeeds_true(self, tmp_path: Path, scenarios_dir: Path) -> None:
        r = self._runner(tmp_path, scenarios_dir)
        sig = ScenarioSignal(type="command_succeeds", value="echo ok")
        assert r.check_signal(sig) is True

    def test_command_succeeds_false(self, tmp_path: Path, scenarios_dir: Path) -> None:
        r = self._runner(tmp_path, scenarios_dir)
        sig = ScenarioSignal(type="command_succeeds", value="exit 42")
        assert r.check_signal(sig) is False

    def test_unknown_signal_type_returns_false(self, tmp_path: Path, scenarios_dir: Path) -> None:
        r = self._runner(tmp_path, scenarios_dir)
        sig = ScenarioSignal(type="unknown_type_xyzzy", value="anything")
        assert r.check_signal(sig) is False

    def test_check_signals_all_pass(self, tmp_path: Path, scenarios_dir: Path) -> None:
        (tmp_path / "a.txt").write_text("hello world")
        r = self._runner(tmp_path, scenarios_dir)
        scenario = Scenario(
            id="x",
            tier="smoke",
            setup=ScenarioSetup(),
            task=ScenarioTask(title="t"),
            expected_signals=[
                ScenarioSignal(type="path_exists", value="a.txt"),
                ScenarioSignal(type="file_contains", value="hello", path="a.txt"),
            ],
            limits=ScenarioLimits(),
        )
        passed, total = r.check_signals(scenario)
        assert passed == 2
        assert total == 2

    def test_check_signals_partial(self, tmp_path: Path, scenarios_dir: Path) -> None:
        (tmp_path / "b.txt").write_text("hello")
        r = self._runner(tmp_path, scenarios_dir)
        scenario = Scenario(
            id="x",
            tier="smoke",
            setup=ScenarioSetup(),
            task=ScenarioTask(title="t"),
            expected_signals=[
                ScenarioSignal(type="path_exists", value="b.txt"),
                ScenarioSignal(type="path_exists", value="missing.txt"),
            ],
            limits=ScenarioLimits(),
        )
        passed, total = r.check_signals(scenario)
        assert passed == 1
        assert total == 2


# ---------------------------------------------------------------------------
# ScenarioRunResult helpers
# ---------------------------------------------------------------------------


class TestScenarioRunResult:
    def test_passed_result(self) -> None:
        r = ScenarioRunResult(
            scenario_id="s1",
            tier="smoke",
            run_index=0,
            passed=True,
            signals_passed=3,
            signals_total=3,
            cost_usd=0.05,
            duration_s=12.3,
        )
        assert r.passed is True
        assert r.failure_reason is None

    def test_failed_result_has_reason(self) -> None:
        r = ScenarioRunResult(
            scenario_id="s1",
            tier="smoke",
            run_index=0,
            passed=False,
            signals_passed=1,
            signals_total=3,
            failure_reason="2/3 signals passed",
        )
        assert r.passed is False
        assert r.failure_reason is not None


# ---------------------------------------------------------------------------
# ScenarioBatchResult helpers
# ---------------------------------------------------------------------------


class TestScenarioBatchResult:
    def _make_result(self, passed: bool) -> ScenarioRunResult:
        return ScenarioRunResult(
            scenario_id="x",
            tier="smoke",
            run_index=0,
            passed=passed,
            signals_passed=3 if passed else 1,
            signals_total=3,
            cost_usd=0.05,
            duration_s=10.0,
        )

    def test_pass_rate_all_pass(self) -> None:
        batch = ScenarioBatchResult(scenario_id="x", tier="smoke")
        batch.runs = [self._make_result(True)] * 3
        assert batch.pass_rate == "3/3"
        assert batch.passed is True

    def test_pass_rate_majority(self) -> None:
        batch = ScenarioBatchResult(scenario_id="x", tier="smoke")
        batch.runs = [
            self._make_result(True),
            self._make_result(True),
            self._make_result(False),
        ]
        assert batch.pass_rate == "2/3"
        assert batch.passed is True

    def test_pass_rate_minority(self) -> None:
        batch = ScenarioBatchResult(scenario_id="x", tier="smoke")
        batch.runs = [
            self._make_result(True),
            self._make_result(False),
            self._make_result(False),
        ]
        assert batch.pass_rate == "1/3"
        assert batch.passed is False

    def test_pass_rate_none_pass(self) -> None:
        batch = ScenarioBatchResult(scenario_id="x", tier="smoke")
        batch.runs = [self._make_result(False)] * 3
        assert batch.pass_rate == "0/3"
        assert batch.passed is False

    def test_empty_batch_not_passed(self) -> None:
        batch = ScenarioBatchResult(scenario_id="x", tier="smoke")
        assert batch.passed is False

    def test_mean_cost_and_duration(self) -> None:
        batch = ScenarioBatchResult(scenario_id="x", tier="smoke")
        batch.runs = [
            ScenarioRunResult("x", "smoke", 0, True, 3, 3, cost_usd=0.10, duration_s=20.0),
            ScenarioRunResult("x", "smoke", 1, True, 3, 3, cost_usd=0.20, duration_s=40.0),
        ]
        assert batch.mean_cost_usd == pytest.approx(0.15)
        assert batch.mean_duration_s == pytest.approx(30.0)

    def test_mean_cost_empty(self) -> None:
        batch = ScenarioBatchResult(scenario_id="x", tier="smoke")
        assert batch.mean_cost_usd == 0.0
        assert batch.mean_duration_s == 0.0


# ---------------------------------------------------------------------------
# run_scenario_once — no executor
# ---------------------------------------------------------------------------


class TestRunScenarioOnce:
    def test_all_signals_pass(self, tmp_path: Path, scenarios_dir: Path) -> None:
        (tmp_path / "out.txt").write_text("expected content")
        r = ScenarioRunner(scenarios_dir=scenarios_dir, repo_root=tmp_path)
        scenario = Scenario(
            id="x",
            tier="smoke",
            setup=ScenarioSetup(command=None),
            task=ScenarioTask(title="t"),
            expected_signals=[
                ScenarioSignal(type="path_exists", value="out.txt"),
                ScenarioSignal(type="file_contains", value="expected", path="out.txt"),
            ],
            limits=ScenarioLimits(),
        )
        result = r.run_scenario_once(scenario, run_index=0)
        assert result.passed is True
        assert result.signals_passed == 2
        assert result.signals_total == 2
        assert result.run_index == 0
        assert result.duration_s >= 0.0

    def test_setup_failure_short_circuits(self, tmp_path: Path, scenarios_dir: Path) -> None:
        r = ScenarioRunner(scenarios_dir=scenarios_dir, repo_root=tmp_path)
        scenario = Scenario(
            id="x",
            tier="smoke",
            setup=ScenarioSetup(command="exit 1"),
            task=ScenarioTask(title="t"),
            expected_signals=[ScenarioSignal(type="path_exists", value="x.txt")],
            limits=ScenarioLimits(),
        )
        result = r.run_scenario_once(scenario)
        assert result.passed is False
        assert result.failure_reason is not None
        assert "setup" in result.failure_reason.lower()

    def test_executor_called(self, tmp_path: Path, scenarios_dir: Path) -> None:
        executed: list[str] = []

        def fake_executor(scenario: Scenario, repo_root: Path) -> float:
            executed.append(scenario.id)
            # Create the expected file as the "agent" would
            (repo_root / "agent_output.txt").write_text("done")
            return 0.05

        r = ScenarioRunner(
            scenarios_dir=scenarios_dir,
            repo_root=tmp_path,
            executor=fake_executor,
        )
        scenario = Scenario(
            id="exec-test",
            tier="smoke",
            setup=ScenarioSetup(),
            task=ScenarioTask(title="t"),
            expected_signals=[ScenarioSignal(type="path_exists", value="agent_output.txt")],
            limits=ScenarioLimits(),
        )
        result = r.run_scenario_once(scenario)
        assert executed == ["exec-test"]
        assert result.passed is True
        assert result.cost_usd == pytest.approx(0.05)

    def test_executor_exception_captured(self, tmp_path: Path, scenarios_dir: Path) -> None:
        def failing_executor(scenario: Scenario, repo_root: Path) -> float:
            raise RuntimeError("agent crashed")

        r = ScenarioRunner(
            scenarios_dir=scenarios_dir,
            repo_root=tmp_path,
            executor=failing_executor,
        )
        scenario = Scenario(
            id="x",
            tier="smoke",
            setup=ScenarioSetup(),
            task=ScenarioTask(title="t"),
            expected_signals=[ScenarioSignal(type="path_exists", value="x.txt")],
            limits=ScenarioLimits(),
        )
        result = r.run_scenario_once(scenario)
        assert result.passed is False
        assert result.failure_reason is not None
        assert "executor" in result.failure_reason.lower()

    def test_no_signals_fails(self, tmp_path: Path, scenarios_dir: Path) -> None:
        r = ScenarioRunner(scenarios_dir=scenarios_dir, repo_root=tmp_path)
        scenario = Scenario(
            id="x",
            tier="smoke",
            setup=ScenarioSetup(),
            task=ScenarioTask(title="t"),
            expected_signals=[],
            limits=ScenarioLimits(),
        )
        result = r.run_scenario_once(scenario)
        assert result.passed is False
        assert result.signals_total == 0


# ---------------------------------------------------------------------------
# run_scenario (batch) and run_all
# ---------------------------------------------------------------------------


class TestRunScenarioBatch:
    def test_run_scenario_returns_batch(self, tmp_path: Path, scenarios_dir: Path) -> None:
        (tmp_path / "README.md").write_text("hello")
        r = ScenarioRunner(scenarios_dir=scenarios_dir, repo_root=tmp_path)
        scenario = runner_load_first_smoke(r)
        batch = r.run_scenario(scenario, runs=2)
        assert len(batch.runs) == 2
        assert batch.scenario_id == scenario.id

    def test_run_all_returns_one_per_scenario(self, tmp_path: Path, scenarios_dir: Path) -> None:
        (tmp_path / "README.md").write_text("hello")
        r = ScenarioRunner(scenarios_dir=scenarios_dir, repo_root=tmp_path)
        results = r.run_all(runs=1)
        assert len(results) == 3

    def test_run_all_tier_filter(self, tmp_path: Path, scenarios_dir: Path) -> None:
        r = ScenarioRunner(scenarios_dir=scenarios_dir, repo_root=tmp_path)
        results = r.run_all(tier="standard", runs=1)
        assert len(results) == 1
        assert results[0].scenario_id == "standard-beta"


def runner_load_first_smoke(r: ScenarioRunner) -> Scenario:
    """Helper: load the first smoke scenario."""
    return r.load_scenarios(tier="smoke")[0]


# ---------------------------------------------------------------------------
# Scenario YAML files on disk
# ---------------------------------------------------------------------------


class TestScenarioFiles:
    """Verify the 20 bundled scenario files parse without errors."""

    _scenarios_dir = Path(__file__).parent.parent.parent / ".sdd" / "eval" / "scenarios"

    @pytest.mark.skipif(
        not _scenarios_dir.exists(),
        reason="Scenarios directory not found",
    )
    def test_all_20_scenarios_load(self) -> None:
        r = ScenarioRunner(
            scenarios_dir=self._scenarios_dir,
            repo_root=Path(__file__).parent.parent.parent,
        )
        scenarios = r.load_scenarios()
        assert len(scenarios) == 20, f"Expected 20 scenarios, found {len(scenarios)}: {[s.id for s in scenarios]}"

    @pytest.mark.skipif(
        not _scenarios_dir.exists(),
        reason="Scenarios directory not found",
    )
    def test_tier_distribution(self) -> None:
        r = ScenarioRunner(
            scenarios_dir=self._scenarios_dir,
            repo_root=Path(__file__).parent.parent.parent,
        )
        scenarios = r.load_scenarios()
        by_tier: dict[str, int] = {}
        for s in scenarios:
            by_tier[s.tier] = by_tier.get(s.tier, 0) + 1
        assert by_tier.get("smoke", 0) == 5, f"Expected 5 smoke, got {by_tier}"
        assert by_tier.get("standard", 0) == 7, f"Expected 7 standard, got {by_tier}"
        assert by_tier.get("stretch", 0) == 5, f"Expected 5 stretch, got {by_tier}"
        assert by_tier.get("adversarial", 0) == 3, f"Expected 3 adversarial, got {by_tier}"

    @pytest.mark.skipif(
        not _scenarios_dir.exists(),
        reason="Scenarios directory not found",
    )
    def test_all_scenarios_have_signals(self) -> None:
        r = ScenarioRunner(
            scenarios_dir=self._scenarios_dir,
            repo_root=Path(__file__).parent.parent.parent,
        )
        scenarios = r.load_scenarios()
        for s in scenarios:
            assert len(s.expected_signals) > 0, f"Scenario {s.id} has no expected_signals"

    @pytest.mark.skipif(
        not _scenarios_dir.exists(),
        reason="Scenarios directory not found",
    )
    def test_all_scenarios_have_unique_ids(self) -> None:
        r = ScenarioRunner(
            scenarios_dir=self._scenarios_dir,
            repo_root=Path(__file__).parent.parent.parent,
        )
        scenarios = r.load_scenarios()
        ids = [s.id for s in scenarios]
        assert len(ids) == len(set(ids)), f"Duplicate IDs found: {ids}"

    @pytest.mark.skipif(
        not _scenarios_dir.exists(),
        reason="Scenarios directory not found",
    )
    def test_smoke_limits_are_cheap(self) -> None:
        r = ScenarioRunner(
            scenarios_dir=self._scenarios_dir,
            repo_root=Path(__file__).parent.parent.parent,
        )
        smoke = r.load_scenarios(tier="smoke")
        for s in smoke:
            assert s.limits.max_cost_usd <= 0.10, (
                f"Smoke scenario {s.id} has max_cost_usd={s.limits.max_cost_usd} > $0.10"
            )
            assert s.limits.max_duration_seconds <= 60, (
                f"Smoke scenario {s.id} has max_duration_seconds={s.limits.max_duration_seconds} > 60"
            )
