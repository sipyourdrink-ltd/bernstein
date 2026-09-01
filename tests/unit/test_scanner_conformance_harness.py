"""The scanner conformance harness can actually replay a step.

The harness shipped without a test that called ``replay_step``, and it could
not be called: it read ``step.ctor_kwargs`` (an attribute the step dataclass
does not define), stripped ``scope`` out of the kwargs it passed to
``scan()`` (whose signature requires it positionally), and ``_replay_one``
assigned to the read-only ``passed`` property. Every case here is one of
those seams, exercised end to end with a recording fake adapter.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from bernstein.adapters.scanner import DeterminismTier, ScannerAdapter, ScanResult, ScanScope
from bernstein.adapters.scanner_conformance import (
    ScannerConformanceHarness,
    ScannerGoldenTranscript,
    ScannerTranscriptStep,
)
from bernstein.adapters.scanner_finding import Finding

_FINDING = Finding(rule="RULE-1", path="src/app.py", severity="high", summary="hardcoded credential")


class _RecordingScanner(ScannerAdapter):
    """Fake scanner that records what ``scan()`` received and returns a canned result."""

    calls: list[dict[str, Any]] = []
    result = ScanResult(findings=[_FINDING], transcript="scan transcript", feed_digest="feed-abc")
    raise_exc: type[Exception] | None = None

    def name(self) -> str:
        return "recording-scanner"

    def scan(self, target: Path, scope: ScanScope, workdir: Path) -> ScanResult:
        type(self).calls.append({"target": target, "scope": scope, "workdir": workdir})
        if type(self).raise_exc is not None:
            raise type(self).raise_exc("scan blew up")
        return type(self).result


_ADAPTER_CLASS = f"{_RecordingScanner.__module__}.{_RecordingScanner.__qualname__}"


def _reset(result: ScanResult | None = None, raise_exc: type[Exception] | None = None) -> None:
    _RecordingScanner.calls = []
    _RecordingScanner.raise_exc = raise_exc
    if result is not None:
        _RecordingScanner.result = result


def test_replay_step_replays_a_plain_step() -> None:
    """A step straight out of ``from_dict`` replays without crashing.

    Regression: ``replay_step`` read ``step.ctor_kwargs``, which the step
    dataclass never defined, so the first replayed step died on
    ``AttributeError`` before the scan was attempted.
    """
    _reset(result=ScanResult(findings=[_FINDING], transcript="t", feed_digest=""))
    step = ScannerTranscriptStep.from_dict({"prompt": "scan it"})

    result = ScannerConformanceHarness().replay_step(_RecordingScanner(), step, 0)

    assert result.passed, result.message
    assert len(_RecordingScanner.calls) == 1


def test_replay_step_passes_the_step_scope_to_scan() -> None:
    """The step's scope reaches ``scan()`` instead of being dropped.

    Regression: the old code computed a scope and then filtered it out of the
    kwargs it forwarded, so ``scan(target, scope, workdir)`` could never have
    received it.
    """
    _reset(result=ScanResult(findings=[], transcript="t", feed_digest=""))
    step = ScannerTranscriptStep.from_dict(
        {
            "target": "src",
            "scope": {"roots": ["src"], "include": ["*.py"], "max_depth": 3},
        }
    )

    ScannerConformanceHarness().replay_step(_RecordingScanner(), step, 0)

    call = _RecordingScanner.calls[0]
    assert call["target"] == Path("src")
    assert call["scope"] == ScanScope(roots=(Path("src"),), include=("*.py",), max_depth=3)


def test_replay_step_flags_a_finding_hash_mismatch() -> None:
    _reset(result=ScanResult(findings=[_FINDING], transcript="t", feed_digest=""))
    step = ScannerTranscriptStep(expected_finding_hashes=["not-the-real-hash"])

    result = ScannerConformanceHarness().replay_step(_RecordingScanner(), step, 0)

    assert not result.passed
    assert "expected hashes" in result.message


def test_replay_step_accepts_matching_finding_hashes() -> None:
    _reset(result=ScanResult(findings=[_FINDING], transcript="t", feed_digest=""))
    step = ScannerTranscriptStep(expected_finding_hashes=[_FINDING.finding_hash()])

    result = ScannerConformanceHarness().replay_step(_RecordingScanner(), step, 0)

    assert result.passed, result.message


def test_replay_step_compares_the_pinned_feed_digest() -> None:
    """``expect_feed_digest`` is compared, not merely parsed and forgotten."""
    _reset(result=ScanResult(findings=[], transcript="t", feed_digest="feed-actual"))
    step = ScannerTranscriptStep(expect_feed_digest="feed-expected")

    result = ScannerConformanceHarness().replay_step(_RecordingScanner(), step, 0)

    assert not result.passed
    assert "feed_digest" in result.message
    assert result.feed_digest == "feed-actual"


def test_replay_step_compares_the_pinned_transcript() -> None:
    """``expected_transcript`` is compared, not merely parsed and forgotten."""
    _reset(result=ScanResult(findings=[], transcript="what actually ran", feed_digest=""))
    step = ScannerTranscriptStep(expected_transcript="what was recorded")

    result = ScannerConformanceHarness().replay_step(_RecordingScanner(), step, 0)

    assert not result.passed
    assert "transcript differs" in result.message


def test_replay_step_matches_an_expected_exception() -> None:
    _reset(raise_exc=RuntimeError)
    step = ScannerTranscriptStep(expect_exception="RuntimeError")

    result = ScannerConformanceHarness().replay_step(_RecordingScanner(), step, 0)

    assert result.passed, result.message


def test_replay_transcript_runs_end_to_end() -> None:
    """``_replay_one`` completes: tier from the adapter name, ``passed`` computed.

    Regression: it looked the tier up under the dotted class path (never a
    registry key) and assigned to the read-only ``passed`` property, so no
    transcript could ever finish replaying.
    """
    _reset(result=ScanResult(findings=[_FINDING], transcript="t", feed_digest=""))
    transcript = ScannerGoldenTranscript(
        name="recording",
        adapter_class=_ADAPTER_CLASS,
        steps=[ScannerTranscriptStep(), ScannerTranscriptStep()],
    )

    result = ScannerConformanceHarness().replay_transcript(transcript)

    assert result.passed
    assert result.adapter_name == "recording-scanner"
    # Unregistered scanners resolve to the weakest tier, same as the registry.
    assert result.determinism_tier is DeterminismTier.TRANSCRIPT_ANCHORED
    assert [s.step_index for s in result.step_results] == [0, 1]


def test_run_all_reports_failed_steps_as_regressions() -> None:
    _reset(result=ScanResult(findings=[], transcript="t", feed_digest=""))
    transcript = ScannerGoldenTranscript(
        name="recording",
        adapter_class=_ADAPTER_CLASS,
        steps=[ScannerTranscriptStep(expected_finding_hashes=["missing-hash"])],
    )

    report = ScannerConformanceHarness().run_all([transcript])

    assert not report.passed
    assert len(report.regressions) == 1
    assert "recording step 0" in report.regressions[0]
