"""Scenario runner — execute YAML-defined eval scenarios against the live codebase.

Each scenario is a small, deterministic task with a known correct outcome. The runner:
1. Loads scenario definitions from YAML files.
2. Runs the setup command to put the codebase in the target state.
3. (Optionally) executes an agent via an injectable executor callable.
4. Evaluates the expected signals to determine pass/fail.
5. Repeats N times and aggregates stochastic pass rates.

Typical usage::

    from pathlib import Path
    from bernstein.eval.scenario_runner import ScenarioRunner

    runner = ScenarioRunner(
        scenarios_dir=Path(".sdd/eval/scenarios"),
        repo_root=Path("."),
    )
    scenarios = runner.load_scenarios(tier="smoke")
    for scenario in scenarios:
        batch = runner.run_scenario(scenario, runs=3)
        print(batch.pass_rate, batch.mean_cost_usd)
"""

from __future__ import annotations

import logging
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, cast

import yaml

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

VALID_TIERS = frozenset({"smoke", "standard", "stretch", "adversarial"})
VALID_SIGNAL_TYPES = frozenset({"file_contains", "test_passes", "command_succeeds", "path_exists", "import_succeeds"})


@dataclass(frozen=True)
class ScenarioSetup:
    """Setup step executed before the agent runs.

    Attributes:
        command: Shell command (run via bash -c) that puts the codebase in the
            target state. May be None if no setup is needed.
    """

    command: str | None = None


@dataclass(frozen=True)
class ScenarioTask:
    """Task description handed to the agent.

    Attributes:
        title: One-line task title.
        description: Detailed description of what the agent must do.
        role: Agent role (backend, qa, docs, …).
        effort: Effort level hint (low, medium, high, max).
        model: Model hint (haiku, sonnet, opus).
    """

    title: str
    description: str = ""
    role: str = "backend"
    effort: str = "low"
    model: str = "sonnet"


@dataclass(frozen=True)
class ScenarioSignal:
    """A single completion signal to evaluate after the agent runs.

    Attributes:
        type: Signal type. One of: file_contains, test_passes,
            command_succeeds, path_exists, import_succeeds.
        value: Primary value for the signal (file content, command, module name).
        path: File path for file_contains / path_exists signals. When omitted
            for file_contains, ``value`` must encode the path as ``"path :: text"``.
    """

    type: str
    value: str
    path: str | None = None


@dataclass(frozen=True)
class ScenarioLimits:
    """Resource limits for a scenario run.

    Attributes:
        max_cost_usd: Maximum allowed cost per single run.
        max_duration_seconds: Wall-clock timeout per single run.
        max_retries: How many automatic retries are allowed on failure.
    """

    max_cost_usd: float = 1.00
    max_duration_seconds: int = 300
    max_retries: int = 0


@dataclass(frozen=True)
class Scenario:
    """A fully-parsed eval scenario.

    Attributes:
        id: Unique scenario ID (slug).
        tier: smoke | standard | stretch | adversarial.
        setup: Setup step run before the agent.
        task: Task description for the agent.
        expected_signals: Signals that must pass for the scenario to pass.
        limits: Resource and retry limits.
    """

    id: str
    tier: str
    setup: ScenarioSetup
    task: ScenarioTask
    expected_signals: list[ScenarioSignal]
    limits: ScenarioLimits


@dataclass
class ScenarioRunResult:
    """Result from a single scenario execution attempt.

    Attributes:
        scenario_id: ID of the scenario.
        tier: Tier the scenario belongs to.
        run_index: 0-based index of this run within the batch.
        passed: True if all expected signals passed.
        signals_passed: Number of signals that passed.
        signals_total: Total signals checked.
        cost_usd: Reported cost (0.0 if unknown/not instrumented).
        duration_s: Wall-clock time for setup + agent execution + signal check.
        failure_reason: Human-readable reason if passed is False.
    """

    scenario_id: str
    tier: str
    run_index: int
    passed: bool
    signals_passed: int
    signals_total: int
    cost_usd: float = 0.0
    duration_s: float = 0.0
    failure_reason: str | None = None


@dataclass
class ScenarioBatchResult:
    """Aggregated result across multiple runs of the same scenario.

    Attributes:
        scenario_id: ID of the scenario.
        tier: Tier the scenario belongs to.
        runs: Individual run results.
    """

    scenario_id: str
    tier: str
    runs: list[ScenarioRunResult] = field(default_factory=list[ScenarioRunResult])

    @property
    def pass_count(self) -> int:
        """Number of runs that passed."""
        return sum(1 for r in self.runs if r.passed)

    @property
    def pass_rate(self) -> str:
        """Pass rate as a fraction string, e.g. '2/3'."""
        return f"{self.pass_count}/{len(self.runs)}"

    @property
    def passed(self) -> bool:
        """True when at least 2/3 of runs succeed (majority)."""
        n = len(self.runs)
        if n == 0:
            return False
        return self.pass_count >= max(1, (n + 1) // 2)

    @property
    def mean_cost_usd(self) -> float:
        """Mean cost across all runs."""
        if not self.runs:
            return 0.0
        return sum(r.cost_usd for r in self.runs) / len(self.runs)

    @property
    def mean_duration_s(self) -> float:
        """Mean wall-clock time across all runs."""
        if not self.runs:
            return 0.0
        return sum(r.duration_s for r in self.runs) / len(self.runs)


# ---------------------------------------------------------------------------
# Agent executor protocol — injectable for testing
# ---------------------------------------------------------------------------


class AgentExecutor(Protocol):
    """Protocol for the agent execution step.

    Implementations spawn an agent (or simulate one) and return the cost incurred.
    """

    def __call__(self, scenario: Scenario, repo_root: Path) -> float:
        """Execute the agent for *scenario* and return cost in USD."""
        ...


# ---------------------------------------------------------------------------
# ScenarioRunner
# ---------------------------------------------------------------------------


class ScenarioRunner:
    """Load and execute YAML-based eval scenarios.

    Args:
        scenarios_dir: Directory containing ``*.yaml`` scenario files.
        repo_root: Root of the repository. Defaults to ``Path(".")``.
        executor: Optional callable that runs the agent for each scenario.
            When ``None`` (default), signal evaluation is performed without
            spawning an agent — useful for testing signal-check logic in
            isolation or for scenarios whose setup already produces the
            expected output.
        command_timeout: Timeout in seconds for individual shell commands
            executed during setup and signal checking. Defaults to 60.
    """

    def __init__(
        self,
        scenarios_dir: Path,
        repo_root: Path | None = None,
        executor: AgentExecutor | None = None,
        command_timeout: int = 60,
    ) -> None:
        self._scenarios_dir = Path(scenarios_dir)
        self._repo_root = Path(repo_root) if repo_root is not None else Path(".")
        self._executor = executor
        self._command_timeout = command_timeout

    # ------------------------------------------------------------------
    # Loading
    # ------------------------------------------------------------------

    def load_scenarios(self, tier: str | None = None) -> list[Scenario]:
        """Load all scenarios from the scenarios directory.

        Args:
            tier: If provided, only load scenarios with this tier value.

        Returns:
            List of Scenario objects sorted by filename.

        Raises:
            FileNotFoundError: If scenarios_dir does not exist.
            ValueError: If a YAML file contains an invalid tier or signal type.
        """
        if not self._scenarios_dir.exists():
            raise FileNotFoundError(f"Scenarios directory not found: {self._scenarios_dir}")
        yaml_files = sorted(self._scenarios_dir.glob("*.yaml"))
        scenarios: list[Scenario] = []
        for path in yaml_files:
            try:
                scenario = self._parse_scenario_file(path)
            except Exception as exc:
                logger.warning("Skipping %s: %s", path.name, exc)
                continue
            if tier is None or scenario.tier == tier:
                scenarios.append(scenario)
        return scenarios

    def load_scenario(self, scenario_id: str) -> Scenario:
        """Load a single scenario by its ID.

        Args:
            scenario_id: The ``id`` field in the YAML file.

        Returns:
            The parsed Scenario.

        Raises:
            FileNotFoundError: If no scenario with the given ID exists.
        """
        for path in sorted(self._scenarios_dir.glob("*.yaml")):
            try:
                scenario = self._parse_scenario_file(path)
            except Exception:
                continue
            if scenario.id == scenario_id:
                return scenario
        raise FileNotFoundError(f"Scenario '{scenario_id}' not found in {self._scenarios_dir}")

    # ------------------------------------------------------------------
    # Setup
    # ------------------------------------------------------------------

    def run_setup(self, scenario: Scenario) -> bool:
        """Execute the setup command for *scenario*.

        Args:
            scenario: Scenario whose setup step to run.

        Returns:
            True if the command succeeded (exit code 0) or there was no command.
        """
        if scenario.setup.command is None:
            return True
        try:
            result = subprocess.run(
                scenario.setup.command,
                shell=True,  # SECURITY: shell=True required because scenario setup
                # commands are developer-authored YAML configs that may use
                # shell features; not user input
                capture_output=True,
                text=True,
                timeout=self._command_timeout,
                cwd=self._repo_root,
            )
            if result.returncode != 0:
                logger.warning(
                    "Setup failed for %s (exit %d): %s",
                    scenario.id,
                    result.returncode,
                    result.stderr[:500],
                )
                return False
            return True
        except subprocess.TimeoutExpired:
            logger.warning("Setup timed out for %s", scenario.id)
            return False
        except Exception as exc:
            logger.warning("Setup error for %s: %s", scenario.id, exc)
            return False

    # ------------------------------------------------------------------
    # Signal evaluation
    # ------------------------------------------------------------------

    def check_signal(self, signal: ScenarioSignal) -> bool:
        """Evaluate a single completion signal.

        Args:
            signal: The signal to check.

        Returns:
            True if the signal condition is satisfied.
        """
        try:
            if signal.type == "file_contains":
                return self._check_file_contains(signal)
            if signal.type == "path_exists":
                return self._check_path_exists(signal)
            if signal.type == "test_passes":
                return self._check_command(signal.value)
            if signal.type == "command_succeeds":
                return self._check_command(signal.value)
            if signal.type == "import_succeeds":
                return self._check_import(signal.value)
            logger.warning("Unknown signal type: %s", signal.type)
            return False
        except Exception as exc:
            logger.debug("Signal check error (%s): %s", signal.type, exc)
            return False

    def check_signals(self, scenario: Scenario) -> tuple[int, int]:
        """Evaluate all expected signals for *scenario*.

        Args:
            scenario: Scenario whose signals to check.

        Returns:
            Tuple of (signals_passed, signals_total).
        """
        total = len(scenario.expected_signals)
        passed = sum(1 for sig in scenario.expected_signals if self.check_signal(sig))
        return passed, total

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------

    def run_scenario_once(self, scenario: Scenario, run_index: int = 0) -> ScenarioRunResult:
        """Run a single execution of *scenario*.

        Lifecycle: setup → (agent execution) → signal check.

        When no executor is configured, the setup is run and signals are
        checked immediately (useful for verifying fixture state without an
        agent).

        Args:
            scenario: The scenario to run.
            run_index: 0-based run index (used in the result for reporting).

        Returns:
            ScenarioRunResult with outcome, signal counts, and timing.
        """
        start_ts = time.monotonic()
        cost_usd = 0.0

        # 1. Setup
        if not self.run_setup(scenario):
            duration = time.monotonic() - start_ts
            return ScenarioRunResult(
                scenario_id=scenario.id,
                tier=scenario.tier,
                run_index=run_index,
                passed=False,
                signals_passed=0,
                signals_total=len(scenario.expected_signals),
                cost_usd=0.0,
                duration_s=duration,
                failure_reason="Setup step failed",
            )

        # 2. Agent execution (optional)
        if self._executor is not None:
            try:
                cost_usd = self._executor(scenario, self._repo_root)
            except Exception as exc:
                duration = time.monotonic() - start_ts
                return ScenarioRunResult(
                    scenario_id=scenario.id,
                    tier=scenario.tier,
                    run_index=run_index,
                    passed=False,
                    signals_passed=0,
                    signals_total=len(scenario.expected_signals),
                    cost_usd=0.0,
                    duration_s=duration,
                    failure_reason=f"Agent executor raised: {exc}",
                )

        # 3. Signal evaluation
        signals_passed, signals_total = self.check_signals(scenario)
        duration = time.monotonic() - start_ts
        passed = signals_passed == signals_total and signals_total > 0
        failure_reason: str | None = None
        if not passed:
            failure_reason = (
                f"{signals_passed}/{signals_total} signals passed" if signals_total > 0 else "No signals defined"
            )

        return ScenarioRunResult(
            scenario_id=scenario.id,
            tier=scenario.tier,
            run_index=run_index,
            passed=passed,
            signals_passed=signals_passed,
            signals_total=signals_total,
            cost_usd=cost_usd,
            duration_s=duration,
            failure_reason=failure_reason,
        )

    def run_scenario(self, scenario: Scenario, runs: int = 3) -> ScenarioBatchResult:
        """Run *scenario* multiple times and aggregate results.

        Per Anthropic eval guidance, each scenario is run 3 times and a scenario
        is "passing" only when at least 2/3 runs succeed.

        Args:
            scenario: The scenario to run.
            runs: Number of executions. Defaults to 3.

        Returns:
            ScenarioBatchResult with all run results.
        """
        batch = ScenarioBatchResult(scenario_id=scenario.id, tier=scenario.tier)
        for i in range(runs):
            result = self.run_scenario_once(scenario, run_index=i)
            batch.runs.append(result)
            logger.info(
                "[%s] run %d/%d — %s (%.2fs)",
                scenario.id,
                i + 1,
                runs,
                "PASS" if result.passed else "FAIL",
                result.duration_s,
            )
        return batch

    def run_all(self, tier: str | None = None, runs: int = 3) -> list[ScenarioBatchResult]:
        """Run all scenarios (optionally filtered by tier) and return results.

        Args:
            tier: If provided, only run scenarios matching this tier.
            runs: Number of executions per scenario. Defaults to 3.

        Returns:
            List of ScenarioBatchResult, one per scenario.
        """
        scenarios = self.load_scenarios(tier=tier)
        results: list[ScenarioBatchResult] = []
        for scenario in scenarios:
            logger.info("Running scenario: %s (%s)", scenario.id, scenario.tier)
            batch = self.run_scenario(scenario, runs=runs)
            results.append(batch)
        return results

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _parse_scenario_file(self, path: Path) -> Scenario:
        """Parse a YAML scenario file into a Scenario dataclass.

        Args:
            path: Path to the YAML file.

        Returns:
            Parsed Scenario.

        Raises:
            ValueError: If required fields are missing or values are invalid.
            yaml.YAMLError: If the file is not valid YAML.
        """
        raw_data: object = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(raw_data, dict):
            raise ValueError(f"{path.name}: top-level must be a mapping")

        raw: dict[str, object] = cast("dict[str, object]", raw_data)

        scenario_id = str(raw.get("id") or path.stem)
        tier = str(raw.get("tier", "smoke"))
        if tier not in VALID_TIERS:
            raise ValueError(f"{path.name}: invalid tier '{tier}'; must be one of {VALID_TIERS}")

        # Setup
        setup_raw_val: object = raw.get("setup") or {}
        setup_dict: dict[str, object] = (
            cast("dict[str, object]", setup_raw_val) if isinstance(setup_raw_val, dict) else {}
        )
        setup_cmd: object = setup_dict.get("command")
        setup = ScenarioSetup(command=str(setup_cmd) if setup_cmd is not None else None)

        # Task
        task_raw_val: object = raw.get("task") or {}
        task_dict: dict[str, object] = cast("dict[str, object]", task_raw_val) if isinstance(task_raw_val, dict) else {}
        task = ScenarioTask(
            title=str(task_dict.get("title", "")),
            description=str(task_dict.get("description", "")),
            role=str(task_dict.get("role", "backend")),
            effort=str(task_dict.get("effort", "low")),
            model=str(task_dict.get("model", "sonnet")),
        )

        # Signals
        signals_raw_val: object = raw.get("expected_signals") or []
        signals_list: list[object] = cast("list[object]", signals_raw_val) if isinstance(signals_raw_val, list) else []
        signals: list[ScenarioSignal] = []
        for sig_raw_item in signals_list:
            sig_dict: dict[str, object] = (
                cast("dict[str, object]", sig_raw_item) if isinstance(sig_raw_item, dict) else {}
            )
            sig_type = str(sig_dict.get("type", ""))
            if sig_type not in VALID_SIGNAL_TYPES:
                raise ValueError(f"{path.name}: unknown signal type '{sig_type}'; must be one of {VALID_SIGNAL_TYPES}")
            sig_path_val: object = sig_dict.get("path")
            signals.append(
                ScenarioSignal(
                    type=sig_type,
                    value=str(sig_dict.get("value", "")),
                    path=str(sig_path_val) if sig_path_val is not None else None,
                )
            )

        # Limits
        limits_raw_val: object = raw.get("limits") or {}
        limits_dict: dict[str, object] = (
            cast("dict[str, object]", limits_raw_val) if isinstance(limits_raw_val, dict) else {}
        )
        limits = ScenarioLimits(
            max_cost_usd=float(str(limits_dict.get("max_cost_usd", 1.00))),
            max_duration_seconds=int(str(limits_dict.get("max_duration_seconds", 300))),
            max_retries=int(str(limits_dict.get("max_retries", 0))),
        )

        return Scenario(
            id=scenario_id,
            tier=tier,
            setup=setup,
            task=task,
            expected_signals=signals,
            limits=limits,
        )

    def _check_file_contains(self, signal: ScenarioSignal) -> bool:
        """Check that a file contains a given substring.

        Args:
            signal: Signal with ``path`` (or ``value`` encoded as ``path :: text``)
                and ``value`` as the text to search for.

        Returns:
            True if the file exists and contains the substring.
        """
        if signal.path:
            file_path = self._repo_root / signal.path
            search_text = signal.value
        else:
            # Legacy format: "path :: text"
            if " :: " in signal.value:
                raw_path, search_text = signal.value.split(" :: ", 1)
                file_path = self._repo_root / raw_path.strip()
            else:
                logger.debug("file_contains signal missing path: %s", signal.value)
                return False

        try:
            content = file_path.read_text(encoding="utf-8")
            return search_text in content
        except OSError:
            return False

    def _check_path_exists(self, signal: ScenarioSignal) -> bool:
        """Check that a path exists.

        Args:
            signal: Signal with ``path`` or ``value`` as the path to check.

        Returns:
            True if the path exists.
        """
        target = signal.path or signal.value
        return (self._repo_root / target).exists()

    def _check_command(self, command: str) -> bool:
        """Run a shell command and return True if it exits with code 0.

        Args:
            command: Shell command to execute.

        Returns:
            True if the command exits with code 0.
        """
        try:
            result = subprocess.run(
                command,
                shell=True,  # SECURITY: shell=True required because eval scenario
                # validation commands are developer-authored shell strings
                # from YAML configs; not user input
                capture_output=True,
                text=True,
                timeout=self._command_timeout,
                cwd=self._repo_root,
            )
            return result.returncode == 0
        except subprocess.TimeoutExpired:
            logger.debug("Command timed out: %s", command[:80])
            return False
        except Exception as exc:
            logger.debug("Command error: %s — %s", command[:80], exc)
            return False

    def _check_import(self, module: str) -> bool:
        """Check that a Python module can be imported.

        Args:
            module: Fully-qualified module name (e.g. ``bernstein.core.models``).

        Returns:
            True if the module imports without error.
        """
        cmd = f'python3 -c "import {module}"'
        return self._check_command(cmd)
