"""Adapter tool contract conformance suite harness.

Provides golden-transcript replay, adapter conformance validation, and
regression detection so protocol drift is caught early.

A *golden transcript* is a YAML/JSON file describing a sequence of
spawn-call inputs and the expected observable outputs (e.g. SpawnResult
fields, raised exceptions).  The harness replays the transcript against a
live adapter (with mocked subprocesses) and flags any deviation.

Usage::

    from bernstein.adapters.conformance import ConformanceHarness, load_golden_transcripts

    transcripts = load_golden_transcripts(Path("tests/golden"))
    harness = ConformanceHarness()
    report = harness.run_all(transcripts)
    if report.regressions:
        print("Conformance failures:", report.regressions)

"""

from __future__ import annotations

import importlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import yaml

from bernstein.adapters.base import CLIAdapter, SpawnResult
from bernstein.core.models import ModelConfig

# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass
class TranscriptStep:
    """One call in a golden transcript.

    Args:
        prompt: Prompt passed to spawn().
        model: Model name passed in ModelConfig.
        expected_pid: Expected PID in the SpawnResult (None = any).
        expect_exception: Exception class name to expect, or None.
        expected_log_suffix: Expected suffix of log_path, or None.
    """

    prompt: str = "do the thing"
    model: str = "sonnet"
    expected_pid: int | None = None
    expect_exception: str | None = None
    expected_log_suffix: str | None = None

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> TranscriptStep:
        """Parse a step from a raw dict.

        Args:
            raw: Dict with step fields from YAML/JSON.

        Returns:
            Parsed TranscriptStep.
        """
        return cls(
            prompt=str(raw.get("prompt", "do the thing")),
            model=str(raw.get("model", "sonnet")),
            expected_pid=raw.get("expected_pid"),
            expect_exception=raw.get("expect_exception"),
            expected_log_suffix=raw.get("expected_log_suffix"),
        )


@dataclass
class GoldenTranscript:
    """A named sequence of transcript steps for one adapter.

    Args:
        name: Human-readable transcript identifier.
        adapter_class: Dotted class path (e.g. ``bernstein.adapters.codex.CodexAdapter``).
        steps: Ordered list of spawn-call scenarios.
        ctor_kwargs: Optional keyword arguments forwarded to the adapter constructor.
    """

    name: str
    adapter_class: str
    steps: list[TranscriptStep]
    ctor_kwargs: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> GoldenTranscript:
        """Parse a golden transcript from a raw dict.

        Args:
            raw: Dict loaded from YAML/JSON.

        Returns:
            Parsed GoldenTranscript.
        """
        steps = [TranscriptStep.from_dict(s) for s in raw.get("steps", [])]
        return cls(
            name=str(raw["name"]),
            adapter_class=str(raw["adapter_class"]),
            steps=steps,
            ctor_kwargs=dict(raw.get("ctor_kwargs") or {}),
        )


@dataclass
class StepResult:
    """Result of replaying one transcript step.

    Args:
        step_index: Zero-based index in the transcript.
        passed: Whether the step conformed to its expected outcome.
        message: Human-readable explanation of success or failure.
    """

    step_index: int
    passed: bool
    message: str


@dataclass
class TranscriptResult:
    """Result of replaying a full golden transcript.

    Args:
        transcript_name: Name of the transcript.
        adapter_class: Class under test.
        step_results: Per-step outcomes.
        passed: True only if all steps passed.
    """

    transcript_name: str
    adapter_class: str
    step_results: list[StepResult] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        """True only when every step passed."""
        return all(s.passed for s in self.step_results)

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a plain dict."""
        return {
            "transcript_name": self.transcript_name,
            "adapter_class": self.adapter_class,
            "passed": self.passed,
            "steps": [
                {"step_index": s.step_index, "passed": s.passed, "message": s.message} for s in self.step_results
            ],
        }


@dataclass
class ConformanceReport:
    """Aggregated result of running all transcripts.

    Args:
        results: Per-transcript outcomes.
        regressions: Transcript names that failed conformance.
    """

    results: list[TranscriptResult] = field(default_factory=list)

    @property
    def regressions(self) -> list[str]:
        """Names of transcripts where conformance failed."""
        return [r.transcript_name for r in self.results if not r.passed]

    @property
    def passed(self) -> bool:
        """True only when every transcript passed."""
        return all(r.passed for r in self.results)

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a plain dict."""
        return {
            "passed": self.passed,
            "regressions": self.regressions,
            "results": [r.to_dict() for r in self.results],
        }


# ---------------------------------------------------------------------------
# Transcript loader
# ---------------------------------------------------------------------------


def load_golden_transcripts(directory: Path) -> list[GoldenTranscript]:
    """Load all golden transcript YAML/JSON files from a directory.

    Files must have ``name`` and ``adapter_class`` keys plus a ``steps`` list.
    Malformed files are skipped with a warning rather than crashing the suite.

    Args:
        directory: Directory to search for ``*.yaml`` and ``*.json`` files.

    Returns:
        Parsed transcripts, sorted by name.
    """
    if not directory.exists():
        return []

    transcripts: list[GoldenTranscript] = []
    for path in sorted(directory.glob("*.yaml")) or []:
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8"))
            if isinstance(raw, dict) and "name" in raw and "adapter_class" in raw:
                transcripts.append(GoldenTranscript.from_dict(raw))
        except Exception:
            pass  # Skip malformed YAML transcript files

    for path in sorted(directory.glob("*.json")) or []:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(raw, dict) and "name" in raw and "adapter_class" in raw:
                transcripts.append(GoldenTranscript.from_dict(raw))
        except Exception:
            pass  # Skip malformed JSON transcript files

    return sorted(transcripts, key=lambda t: t.name)


# ---------------------------------------------------------------------------
# Adapter instantiation helper
# ---------------------------------------------------------------------------


def _load_adapter(dotted_class: str, ctor_kwargs: dict[str, Any] | None = None) -> CLIAdapter:
    """Import and instantiate a CLIAdapter by dotted class path.

    Args:
        dotted_class: E.g. ``bernstein.adapters.codex.CodexAdapter``.
        ctor_kwargs: Optional keyword arguments for the constructor.

    Returns:
        A CLIAdapter instance.

    Raises:
        ImportError: If the module cannot be imported.
        AttributeError: If the class is not found in the module.
        TypeError: If the class cannot be instantiated with the given kwargs.
    """
    parts = dotted_class.rsplit(".", 1)
    if len(parts) != 2:
        raise ImportError(f"Invalid dotted class path: {dotted_class!r}")
    module = importlib.import_module(parts[0])
    cls = getattr(module, parts[1])
    return cls(**(ctor_kwargs or {}))


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


def _popen_target_for(adapter: CLIAdapter) -> str:
    """Return the subprocess.Popen patch target for a given adapter."""
    return f"{type(adapter).__module__}.subprocess.Popen"


def _make_popen_mock(pid: int) -> MagicMock:
    m = MagicMock()
    m.pid = pid
    m.stdout = MagicMock()
    return m


class ConformanceHarness:
    """Replay golden transcripts against adapters and detect regressions.

    Each step is replayed by calling ``adapter.spawn()`` with a mocked
    ``subprocess.Popen`` that returns a controlled PID.  The step passes
    when the observed outcome matches the transcript expectation.
    """

    @staticmethod
    def _check_exception_result(
        step: TranscriptStep,
        step_index: int,
        exc: Exception,
    ) -> StepResult:
        """Produce a StepResult for an exception raised during spawn."""
        exc_name = type(exc).__name__
        if step.expect_exception:
            if exc_name == step.expect_exception:
                return StepResult(step_index=step_index, passed=True, message=f"Expected {exc_name} raised")
            return StepResult(
                step_index=step_index,
                passed=False,
                message=f"Expected {step.expect_exception}, got {exc_name}: {exc}",
            )
        return StepResult(
            step_index=step_index,
            passed=False,
            message=f"Unexpected exception {exc_name}: {exc}",
        )

    @staticmethod
    def _validate_spawn_result(
        result: object,
        step: TranscriptStep,
        step_index: int,
    ) -> StepResult:
        """Validate a successful SpawnResult against the transcript step."""
        if step.expect_exception:
            return StepResult(
                step_index=step_index,
                passed=False,
                message=f"Expected {step.expect_exception} but spawn() succeeded (pid={getattr(result, 'pid', '?')})",
            )
        if not isinstance(result, SpawnResult):
            return StepResult(
                step_index=step_index,
                passed=False,
                message=f"spawn() returned {type(result).__name__}, expected SpawnResult",
            )
        if not isinstance(result.pid, int):
            return StepResult(
                step_index=step_index,
                passed=False,
                message=f"SpawnResult.pid is {type(result.pid).__name__}, expected int",
            )
        if not isinstance(result.log_path, Path):
            return StepResult(
                step_index=step_index,
                passed=False,
                message=f"SpawnResult.log_path is {type(result.log_path).__name__}, expected Path",
            )
        if step.expected_log_suffix and not str(result.log_path).endswith(step.expected_log_suffix):
            return StepResult(
                step_index=step_index,
                passed=False,
                message=(
                    f"log_path {result.log_path!s} does not end with expected suffix {step.expected_log_suffix!r}"
                ),
            )
        return StepResult(step_index=step_index, passed=True, message=f"OK — pid={result.pid}")

    def replay_step(
        self,
        adapter: CLIAdapter,
        step: TranscriptStep,
        step_index: int,
        workdir: Path,
    ) -> StepResult:
        """Replay a single transcript step against an adapter.

        Args:
            adapter: The adapter under test.
            step: The transcript step to replay.
            step_index: Zero-based position in the transcript.
            workdir: Temporary working directory for spawn.

        Returns:
            StepResult indicating pass/fail with a message.
        """
        pid = step.expected_pid if step.expected_pid is not None else 1234
        popen_target = _popen_target_for(adapter)
        side_effects = [_make_popen_mock(pid), _make_popen_mock(pid + 1)]

        try:
            try:
                ctx = patch(popen_target, side_effect=side_effects)
                ctx.__enter__()
                patched = True
            except (AttributeError, ModuleNotFoundError):
                ctx = None
                patched = False

            try:
                result = adapter.spawn(
                    prompt=step.prompt,
                    workdir=workdir,
                    model_config=ModelConfig(model=step.model, effort="low"),
                    session_id=f"conformance-{step_index}",
                )
            finally:
                if patched and ctx is not None:
                    ctx.__exit__(None, None, None)
        except Exception as exc:
            return self._check_exception_result(step, step_index, exc)

        return self._validate_spawn_result(result, step, step_index)

    def replay_transcript(
        self,
        transcript: GoldenTranscript,
        workdir: Path,
        ctor_kwargs: dict[str, Any] | None = None,
    ) -> TranscriptResult:
        """Replay all steps in a golden transcript.

        Args:
            transcript: The transcript to replay.
            workdir: Temporary directory for spawn calls.
            ctor_kwargs: Optional kwargs forwarded to the adapter constructor.

        Returns:
            TranscriptResult with per-step outcomes.
        """
        result = TranscriptResult(transcript_name=transcript.name, adapter_class=transcript.adapter_class)

        merged_kwargs = dict(transcript.ctor_kwargs)
        if ctor_kwargs:
            merged_kwargs.update(ctor_kwargs)

        try:
            adapter = _load_adapter(transcript.adapter_class, merged_kwargs or None)
        except Exception as exc:
            result.step_results.append(
                StepResult(step_index=0, passed=False, message=f"Failed to instantiate adapter: {exc}")
            )
            return result

        for i, step in enumerate(transcript.steps):
            step_result = self.replay_step(adapter, step, i, workdir)
            result.step_results.append(step_result)

        return result

    def run_all(
        self,
        transcripts: list[GoldenTranscript],
        workdir: Path | None = None,
    ) -> ConformanceReport:
        """Run all transcripts and aggregate into a report.

        Args:
            transcripts: Transcripts to replay.
            workdir: Directory for spawn calls (uses a temp dir if None is given by caller).

        Returns:
            ConformanceReport with regressions identified.
        """
        import tempfile

        report = ConformanceReport()
        with tempfile.TemporaryDirectory() as tmp:
            wd = workdir or Path(tmp)
            for transcript in transcripts:
                result = self.replay_transcript(transcript, workdir=wd)
                report.results.append(result)

        return report


# ---------------------------------------------------------------------------
# Plugin scaffold generator
# ---------------------------------------------------------------------------


ADAPTER_SCAFFOLD_TEMPLATE = '''\
"""Adapter for {cli_name}."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from bernstein.adapters.base import CLIAdapter, SpawnResult
from bernstein.core.models import ApiTierInfo, ModelConfig


class {class_name}(CLIAdapter):
    """Adapter for the {cli_name} CLI agent."""

    def name(self) -> str:
        """Return the adapter identifier."""
        return "{adapter_id}"

    def spawn(
        self,
        *,
        prompt: str,
        workdir: Path,
        model_config: ModelConfig,
        session_id: str,
        mcp_config: dict[str, Any] | None = None,
        timeout_seconds: int = 1800,
        task_scope: str = "medium",
    ) -> SpawnResult:
        """Launch {cli_name} with the given prompt.

        Args:
            prompt: Task description for the agent.
            workdir: Directory to run the agent in.
            model_config: Model selection and effort level.
            session_id: Unique session identifier.
            mcp_config: Optional MCP tool configuration.
            timeout_seconds: Maximum runtime before kill.

        Returns:
            SpawnResult with PID and log path.
        """
        log_path = workdir / f"{adapter_id}-{{session_id}}.log"
        cmd = ["{cli_command}", "--prompt", prompt]
        proc = subprocess.Popen(
            cmd,
            cwd=workdir,
            stdout=open(log_path, "w"),
            stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace",
        )
        return SpawnResult(pid=proc.pid, log_path=log_path, proc=proc)
'''


def generate_adapter_scaffold(
    cli_name: str,
    class_name: str,
    adapter_id: str,
    cli_command: str,
) -> str:
    """Generate source code for a new adapter following the CLIAdapter contract.

    Args:
        cli_name: Human-readable CLI tool name (e.g. ``MyAgent``).
        class_name: Python class name (e.g. ``MyAgentAdapter``).
        adapter_id: Short identifier returned by ``name()`` (e.g. ``myagent``).
        cli_command: Shell command to invoke the tool (e.g. ``myagent``).

    Returns:
        Python source code string for the new adapter module.
    """
    return ADAPTER_SCAFFOLD_TEMPLATE.format(
        cli_name=cli_name,
        class_name=class_name,
        adapter_id=adapter_id,
        cli_command=cli_command,
    )
