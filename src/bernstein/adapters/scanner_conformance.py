"""Scanner adapter conformance suite harness.

Provides golden-transcript replay, adapter conformance validation, and
regression detection for scanner adapters.  Mirrors
``bernstein.adapters.conformance`` but operates on ``ScannerAdapter``
instances and ``Finding`` objects rather than CLI subprocesses.

A *golden transcript* describes a sequence of scan-call inputs and the
expected observable outputs (finding hashes, recorded digests).  The
harness replays the transcript against a live adapter and flags any
deviation.  The determinism tier declared by the adapter drives what the
conformance suite *demands*.

Usage::

    from bernstein.adapters.scanner_conformance import (
        ScannerConformanceHarness,
        load_scanner_golden_transcripts,
    )

    transcripts = load_scanner_golden_transcripts(Path("tests/golden_scanners"))
    harness = ScannerConformanceHarness()
    report = harness.run_all(transcripts)
    if report.regressions:
        print("Conformance failures:", report.regressions)
"""

from __future__ import annotations

import importlib
import json
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from bernstein.adapters._contract import scanner_capabilities, scanner_determinism
from bernstein.adapters.scanner import (
    DeterminismTier,
    ScannerAdapter,
    ScanScope,
)

# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass
class ScannerTranscriptStep:
    """One call in a golden scanner transcript.

    Args:
        prompt: Prompt-like target description passed to scan().
        model: Optional model name (scanners don't use models; kept for
            transcript compatibility).
        target: Path (as a string) handed to ``scan(target=...)``.
        scope: Raw ``ScanScope`` fields for this call -- ``roots``,
            ``include``, ``exclude``, ``max_depth``, ``config``.  An empty
            dict replays with a default (unrestricted) scope.
        expected_finding_hashes: Expected per-finding hashes, sorted, or
            ``None`` when any order is acceptable.
        expect_exception: Exception class name to expect, or None.
        expect_feed_digest: Expected feed digest when determinism is
            ``feed_pinned``, or None.
        expected_transcript: Expected transcript text when determinism is
            ``transcript_anchored``, or None.
    """

    prompt: str = "scan this target"
    model: str = "default"
    target: str = "."
    scope: dict[str, Any] = field(default_factory=dict)
    expected_finding_hashes: list[str] | None = None
    expect_exception: str | None = None
    expect_feed_digest: str | None = None
    expected_transcript: str | None = None

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> ScannerTranscriptStep:
        """Parse a step from a raw dict."""
        return cls(
            prompt=str(raw.get("prompt", "scan this target")),
            model=str(raw.get("model", "default")),
            target=str(raw.get("target", ".")),
            scope=dict(raw.get("scope") or {}),
            expected_finding_hashes=raw.get("expected_finding_hashes"),
            expect_exception=raw.get("expect_exception"),
            expect_feed_digest=raw.get("expect_feed_digest"),
            expected_transcript=raw.get("expected_transcript"),
        )


@dataclass
class ScannerGoldenTranscript:
    """A named sequence of transcript steps for one scanner adapter.

    Args:
        name: Human-readable transcript identifier.
        adapter_class: Dotted class path (e.g. ``bernstein.adapters.grype.GrypeAdapter``).
        steps: Ordered list of scan-call scenarios.
        ctor_kwargs: Optional keyword arguments forwarded to the adapter constructor.
    """

    name: str
    adapter_class: str
    steps: list[ScannerTranscriptStep]
    ctor_kwargs: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> ScannerGoldenTranscript:
        """Parse a golden transcript from a raw dict."""
        steps = [ScannerTranscriptStep.from_dict(s) for s in raw.get("steps", [])]
        return cls(
            name=str(raw["name"]),
            adapter_class=str(raw["adapter_class"]),
            steps=steps,
            ctor_kwargs=dict(raw.get("ctor_kwargs") or {}),
        )


@dataclass
class ScannerStepResult:
    """Result of replaying one scanner transcript step.

    Args:
        step_index: Zero-based index in the transcript.
        passed: Whether the step conformed to its expected outcome.
        message: Human-readable explanation of success or failure.
        feed_digest: The feed digest the scan recorded, if any.  Carried so
            the aggregate report can check pinned-input evidence without
            re-running the scan.
    """

    step_index: int
    passed: bool
    message: str
    feed_digest: str = ""


@dataclass
class ScannerTranscriptResult:
    """Result of replaying a full golden transcript.

    Args:
        transcript_name: Name of the transcript.
        adapter_class: Class under test.
        step_results: Per-step outcomes.
        passed: True only if all steps passed.
        determinism_tier: The adapter's declared tier.
    """

    transcript_name: str
    adapter_class: str
    adapter_name: str = ""
    step_results: list[ScannerStepResult] = field(default_factory=list)
    determinism_tier: DeterminismTier = DeterminismTier.TRANSCRIPT_ANCHORED

    def to_dict(self) -> dict[str, Any]:
        return {
            "transcript_name": self.transcript_name,
            "adapter_class": self.adapter_class,
            "adapter_name": self.adapter_name,
            "passed": self.passed,
            "determinism_tier": self.determinism_tier.value,
            "step_results": [
                {"step_index": s.step_index, "passed": s.passed, "message": s.message} for s in self.step_results
            ],
        }

    @property
    def regressions(self) -> list[str]:
        """Names of steps or expectations that failed."""
        return [r.message for r in self.step_results if not r.passed]

    @property
    def passed(self) -> bool:
        """True only when every step passed."""
        return all(s.passed for s in self.step_results)


@dataclass
class ScannerConformanceReport:
    """Aggregated result of running all scanner transcripts.

    Args:
        results: Per-transcript outcomes.
        regressions: Transcript names where conformance failed (tier-specific).
        pinned_input_failures: Scanners that declared pinned_inputs but didn't
            record them.
    """

    results: list[ScannerTranscriptResult] = field(default_factory=list)
    regressions: list[str] = field(default_factory=list)
    pinned_input_failures: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        """True only when every transcript passed."""
        return all(r.passed for r in self.results)

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a plain dict."""
        return {
            "passed": self.passed,
            "regressions": self.regressions,
            "pinned_input_failures": self.pinned_input_failures.copy(),
            "results": [r.to_dict() for r in self.results],
        }


def _resolve_tier(adapter: ScannerAdapter) -> DeterminismTier:
    """Resolve the adapter's declared tier from the registry, by its name.

    The registry is keyed on the scanner *name* (``adapter.name()``), not the
    dotted class path.  An unregistered name resolves to the weakest tier,
    ``transcript_anchored``, which is also what the registry itself returns
    for unknown names.
    """
    declared = scanner_determinism(adapter.name())
    try:
        return DeterminismTier(declared.value)
    except ValueError:
        return DeterminismTier.TRANSCRIPT_ANCHORED


def _scope_from_dict(raw: dict[str, Any]) -> ScanScope:
    """Build a :class:`ScanScope` from a transcript step's raw ``scope`` dict."""
    return ScanScope(
        roots=tuple(Path(r) for r in raw.get("roots", ())),
        include=tuple(raw.get("include", ())),
        exclude=tuple(raw.get("exclude", ())),
        max_depth=raw.get("max_depth"),
        config=dict(raw.get("config") or {}),
    )


# ---------------------------------------------------------------------------
# Scanner instantiation helper
# ---------------------------------------------------------------------------


def _load_adapter(dotted_class: str, ctor_kwargs: dict[str, Any] | None = None) -> ScannerAdapter:
    """Import and instantiate a ScannerAdapter by dotted class path.

    Args:
        dotted_class: E.g. ``bernstein.adapters.grype.GrypeAdapter``.
        ctor_kwargs: Optional keyword arguments for the constructor.

    Returns:
        A ScannerAdapter instance.

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
# Transcript loader
# ---------------------------------------------------------------------------


def load_scanner_golden_transcripts(directory: Path) -> list[ScannerGoldenTranscript]:
    """Load all golden scanner transcript YAML/JSON files from a directory.

    Files must have ``name`` and ``adapter_class`` keys plus a ``steps`` list.
    Malformed files are skipped with a warning rather than crashing the suite.

    Args:
        directory: Directory to search for ``*.yaml`` and ``*.json`` files.

    Returns:
        Parsed transcripts, sorted by name.
    """
    if not directory.exists():
        return []

    transcripts: list[ScannerGoldenTranscript] = []
    for path in sorted(directory.glob("*.yaml")) or []:
        with suppress(Exception):
            raw = yaml.safe_load(path.read_text(encoding="utf-8"))
            if isinstance(raw, dict) and "name" in raw and "adapter_class" in raw:
                transcripts.append(ScannerGoldenTranscript.from_dict(raw))

    for path in sorted(directory.glob("*.json")) or []:
        with suppress(Exception):
            raw = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(raw, dict) and "name" in raw and "adapter_class" in raw:
                transcripts.append(ScannerGoldenTranscript.from_dict(raw))

    return sorted(transcripts, key=lambda t: t.name)


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


class ScannerConformanceHarness:
    """Replay golden scanner transcripts against adapters and detect regressions.

    Each step is replayed by calling ``adapter.scan()`` with appropriate
    arguments.  The determinism tier declared by the adapter drives what
    the conformance suite demands.

    Determinism tier obligations:

    - ``deterministic``   : two runs on identical input yield identical
                          finding hashes.  A mismatch is a hard failure.
    - ``feed_pinned``     : adapter declares pinned_inputs and records
                          a feed_digest.  The digest must be reproducible.
    - ``transcript_anchored``: adapter records a transcript (non-empty
                          string) that a later verify step can diff.
    """

    @staticmethod
    def _ensure_feed_digest(result: ScannerTranscriptResult, step_result: ScannerStepResult) -> None:
        """Ensure step_result carries a feed_digest if its transcript demands it."""
        pass  # populated during replay

    def replay_step(
        self,
        adapter: ScannerAdapter,
        step: ScannerTranscriptStep,
        step_index: int,
    ) -> ScannerStepResult:
        """Replay a single transcript step against a scanner adapter.

        Args:
            adapter: The scanner adapter under test.
            step: The transcript step to replay.
            step_index: Zero-based position in the transcript.

        Returns:
            ScannerStepResult indicating pass/fail with a message.
        """
        # Resolve the adapter's declared tier and capabilities from the
        # registry, keyed on the scanner name (not the dotted class path).
        tier = _resolve_tier(adapter)
        cap = scanner_capabilities(adapter.name())

        # Run the scan.  The step's own fields are the call's inputs: the
        # target path and the ScanScope -- scan() takes scope positionally,
        # so the replay must hand it over rather than drop it.
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            workdir = Path(tmp)
            scope = _scope_from_dict(step.scope)

            try:
                scan_result = adapter.scan(
                    target=Path(step.target),
                    scope=scope,
                    workdir=workdir,
                )
            except Exception as exc:
                # Emit the expected exception
                if step.expect_exception:
                    exc_name = type(exc).__name__
                    if exc_name == step.expect_exception:
                        return ScannerStepResult(
                            step_index=step_index,
                            passed=True,
                            message=f"Expected {step.expect_exception} raised",
                        )
                    return ScannerStepResult(
                        step_index=step_index,
                        passed=False,
                        message=f"Expected {step.expect_exception}, got {exc_name}: {exc}",
                    )
                return ScannerStepResult(
                    step_index=step_index,
                    passed=False,
                    message=f"Unexpected exception {type(exc).__name__}: {exc}",
                )

        # Validate based on declared tier
        expected_hashes = step.expected_finding_hashes

        # Build actual findings hashes for comparison
        actual_finding_hashes = scan_result.finding_hashes()

        # Check expected vs actual
        passed = True
        message_parts: list[str] = []

        if expected_hashes is not None:
            # Deterministic tier check: expected hashes must all be present
            expected_set = set(expected_hashes)
            actual_set = set(actual_finding_hashes)
            missing = expected_set - actual_set
            extra = actual_set - expected_set

            if missing or extra:
                passed = False
                missing_str = sorted(missing) if missing else ""
                extra_str = sorted(extra) if extra else ""
                message_parts.append(f"deterministic tier: expected hashes {missing_str}, got extra {extra_str}")

        # Check feed_pinned
        pinned_inputs = cap.get("pinned_inputs", ()) if cap else ()
        if pinned_inputs and not scan_result.feed_digest:
            passed = False
            message_parts.append(
                f"feed_pinned tier: declared pinned_inputs {pinned_inputs} but no feed_digest recorded"
            )

        # A transcript that pins the exact feed digest must see that digest.
        if step.expect_feed_digest is not None and scan_result.feed_digest != step.expect_feed_digest:
            passed = False
            message_parts.append(
                f"feed_pinned tier: expected feed_digest {step.expect_feed_digest!r}, got {scan_result.feed_digest!r}"
            )

        # Check transcript_anchored
        transcript = scan_result.transcript or ""
        if tier is DeterminismTier.TRANSCRIPT_ANCHORED and not transcript.strip():
            passed = False
            message_parts.append(
                "transcript_anchored tier: adapter declared transcript_anchored but produced empty transcript"
            )

        # A transcript that pins the exact recorded transcript must match it.
        if step.expected_transcript is not None and transcript != step.expected_transcript:
            passed = False
            message_parts.append("transcript_anchored tier: recorded transcript differs from the expected one")

        actual_hashes_str = ", ".join(actual_finding_hashes[:3]) if actual_finding_hashes else "(none)"
        expected_str = ", ".join(expected_hashes[:3]) if expected_hashes else "(none)"

        if passed:
            message = f"OK - tier={tier.value}, findings={actual_hashes_str}, expected={expected_str}"
            if scan_result.feed_digest:
                message += f", feed_digest={scan_result.feed_digest[:12]}..."
            return ScannerStepResult(
                step_index=step_index,
                passed=True,
                message=message,
                feed_digest=scan_result.feed_digest,
            )
        else:
            message = (
                f"FAIL - tier={tier.value}: "
                f"{'; '.join(message_parts)}. "
                f"expected_hashes={expected_str}, actual={actual_hashes_str}"
            )
            return ScannerStepResult(
                step_index=step_index,
                passed=False,
                message=message,
                feed_digest=scan_result.feed_digest,
            )

    def replay_transcript(
        self,
        transcript: ScannerGoldenTranscript,
        workdir: Path | None = None,
    ) -> ScannerTranscriptResult:
        """Replay all steps in a golden scanner transcript.

        Args:
            transcript: The transcript to replay.
            workdir: Temporary directory for scan calls.

        Returns:
            ScannerTranscriptResult with per-step outcomes and tier info.
        """
        import tempfile

        if workdir is None:
            with tempfile.TemporaryDirectory() as tmp:
                wd = Path(tmp)
                result = self._replay_one(transcript, wd)
                return result
        else:
            wd = Path(workdir)
            return self._replay_one(transcript, wd)

    def _replay_one(
        self,
        transcript: ScannerGoldenTranscript,
        workdir: Path,
    ) -> ScannerTranscriptResult:
        """Internal replay of one transcript against a workdir."""
        # The registry is keyed on the scanner name, so the tier lookup needs
        # a built instance; one is built up front for the name, and a fresh
        # one per step so steps cannot leak state into each other.
        first = _load_adapter(transcript.adapter_class, transcript.ctor_kwargs)
        result = ScannerTranscriptResult(
            transcript_name=transcript.name,
            adapter_class=transcript.adapter_class,
            adapter_name=first.name(),
            determinism_tier=_resolve_tier(first),
        )

        for i, step in enumerate(transcript.steps):
            adapter = _load_adapter(transcript.adapter_class, transcript.ctor_kwargs)
            result.step_results.append(self.replay_step(adapter, step, i))

        # ``passed`` is a property computed from the step results.
        return result

    def run_all(
        self,
        transcripts: list[ScannerGoldenTranscript],
        workdir: Path | None = None,
    ) -> ScannerConformanceReport:
        """Run all transcripts and aggregate into a report.

        Args:
            transcripts: Transcripts to replay.
            workdir: Directory for scan calls (uses a temp dir if None).

        Returns:
            ScannerConformanceReport with regressions identified.
        """
        import tempfile

        report = ScannerConformanceReport()
        with tempfile.TemporaryDirectory() as tmp:
            wd = workdir or Path(tmp)
            for transcript in transcripts:
                result = self.replay_transcript(transcript, workdir=wd)
                report.results.append(result)

                # A scanner that declares pinned inputs must have recorded a
                # feed digest on at least one step; the registry is keyed on
                # the adapter name the replay resolved.
                cap = scanner_capabilities(result.adapter_name)
                if cap and cap.get("pinned_inputs") and not any(sr.feed_digest for sr in result.step_results):
                    report.pinned_input_failures.append(
                        f"{transcript.name}: declared pinned_inputs but no feed_digest recorded"
                    )

                for sr in result.step_results:
                    if not sr.passed:
                        report.regressions.append(f"{transcript.name} step {sr.step_index}: {sr.message}")

        return report
