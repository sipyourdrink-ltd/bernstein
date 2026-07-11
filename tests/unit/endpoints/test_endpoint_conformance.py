"""Conformance subset tests for OpenAI-compatible endpoint profiles (issue #2356).

AC coverage:

* AC2 -- the conformance run deterministically certifies or rejects an
  endpoint per role, with machine reason codes for every rejection.
"""

from __future__ import annotations

from bernstein.core.endpoints.conformance import (
    ALL_PROBES,
    CONFORMANCE_SUITE_VERSION,
    LOCAL_TIER_ROLES,
    PROBE_CHAT_COMPLETION,
    PROBE_CONTEXT_FLOOR,
    PROBE_PATCH_FIDELITY,
    PROBE_REACHABILITY,
    PROBE_TIMEOUT_BEHAVIOR,
    PROBE_TOOL_CALLING,
    ConformanceTranscript,
    ProbeResult,
    discover_default_model,
    evaluate_roles,
    is_gated_role,
    normalize_base_url,
    required_probes_for_role,
    run_conformance,
)
from tests.unit.endpoints.stub_endpoint import EndpointBehavior, FakeTransport

_BASE_URL = "http://127.0.0.1:11434/v1"


def _run(behavior: EndpointBehavior | None = None) -> ConformanceTranscript:
    transport = FakeTransport(behavior)
    return run_conformance(
        base_url=_BASE_URL,
        model=transport.behavior.model,
        transport=transport,
    )


def _result(transcript: ConformanceTranscript, probe: str) -> ProbeResult:
    matches = [r for r in transcript.results if r.probe == probe]
    assert len(matches) == 1, f"expected exactly one {probe} result"
    return matches[0]


# ---------------------------------------------------------------------------
# Probe subset
# ---------------------------------------------------------------------------


def test_conformant_endpoint_passes_every_probe() -> None:
    transcript = _run()
    assert transcript.suite_version == CONFORMANCE_SUITE_VERSION
    assert tuple(r.probe for r in transcript.results) == ALL_PROBES
    assert all(r.passed for r in transcript.results)
    assert all(r.reason == "" for r in transcript.results)


def test_missing_tool_call_rejects_tool_probe_with_reason() -> None:
    transcript = _run(EndpointBehavior(tools_ok=False))
    result = _result(transcript, PROBE_TOOL_CALLING)
    assert result.passed is False
    assert result.reason == "no_tool_call"


def test_corrupted_patch_rejects_patch_probe_with_reason() -> None:
    transcript = _run(EndpointBehavior(patch_ok=False))
    result = _result(transcript, PROBE_PATCH_FIDELITY)
    assert result.passed is False
    assert result.reason == "patch_corrupted"


def test_context_floor_rejection_records_reason() -> None:
    transcript = _run(EndpointBehavior(context_ok=False))
    result = _result(transcript, PROBE_CONTEXT_FLOOR)
    assert result.passed is False
    assert result.reason == "context_rejected"


def test_hanging_endpoint_records_timed_out() -> None:
    transcript = _run(EndpointBehavior(hang=True))
    result = _result(transcript, PROBE_TIMEOUT_BEHAVIOR)
    assert result.passed is False
    assert result.reason == "timed_out"


def test_unreachable_endpoint_fails_probes_with_unreachable_reason() -> None:
    def transport(method: str, url: str, headers: object, body: object, timeout: float) -> tuple[int, bytes]:
        raise OSError("connection refused")

    transcript = run_conformance(base_url=_BASE_URL, model="tiny-coder", transport=transport)
    reach = _result(transcript, PROBE_REACHABILITY)
    assert reach.passed is False
    assert reach.reason == "unreachable"
    chat = _result(transcript, PROBE_CHAT_COMPLETION)
    assert chat.passed is False
    assert chat.reason == "unreachable"


def test_failed_chat_backend_records_chat_failed() -> None:
    transcript = _run(EndpointBehavior(chat_ok=False))
    result = _result(transcript, PROBE_CHAT_COMPLETION)
    assert result.passed is False
    assert result.reason == "chat_failed"


# ---------------------------------------------------------------------------
# Determinism (AC2): the verdict is a pure function of the responses
# ---------------------------------------------------------------------------


def test_two_runs_against_same_endpoint_produce_identical_transcripts() -> None:
    behavior = EndpointBehavior(tools_ok=False)
    first = run_conformance(base_url=_BASE_URL, model="tiny-coder", transport=FakeTransport(behavior))
    second = run_conformance(base_url=_BASE_URL, model="tiny-coder", transport=FakeTransport(behavior))
    assert first.to_dict() == second.to_dict()
    assert first.transcript_hash() == second.transcript_hash()
    assert first.transcript_hash().startswith("sha256:")


def test_transcript_round_trips_through_dict() -> None:
    transcript = _run()
    clone = ConformanceTranscript.from_dict(transcript.to_dict())
    assert clone == transcript
    assert clone.transcript_hash() == transcript.transcript_hash()


# ---------------------------------------------------------------------------
# Role policy presets
# ---------------------------------------------------------------------------


def test_local_tier_roles_are_not_gated() -> None:
    assert frozenset({"linter", "test_writer", "triage", "doc_sweeper"}) == LOCAL_TIER_ROLES
    for role in LOCAL_TIER_ROLES:
        assert is_gated_role(role) is False


def test_unknown_roles_are_gated_fail_closed() -> None:
    for role in ("manager", "planner", "backend", "default", "anything-else"):
        assert is_gated_role(role) is True


def test_gated_roles_require_every_probe() -> None:
    assert required_probes_for_role("manager") == frozenset(ALL_PROBES)


def test_low_stakes_roles_do_not_require_tool_probes() -> None:
    required = required_probes_for_role("linter")
    assert PROBE_TOOL_CALLING not in required
    assert PROBE_PATCH_FIDELITY not in required
    assert PROBE_CHAT_COMPLETION in required
    assert PROBE_CONTEXT_FLOOR in required


def test_test_writer_requires_tool_calling_and_patch_fidelity() -> None:
    required = required_probes_for_role("test_writer")
    assert PROBE_TOOL_CALLING in required
    assert PROBE_PATCH_FIDELITY in required


# ---------------------------------------------------------------------------
# Role evaluation (AC2): per-role verdicts with reasons
# ---------------------------------------------------------------------------


def test_evaluate_roles_certifies_and_rejects_per_role() -> None:
    transcript = _run(EndpointBehavior(tools_ok=False))
    verdicts = {v.role: v for v in evaluate_roles(transcript, ["linter", "test_writer", "manager"])}

    assert verdicts["linter"].certified is True
    assert verdicts["linter"].reasons == ()

    assert verdicts["test_writer"].certified is False
    assert any(PROBE_TOOL_CALLING in reason for reason in verdicts["test_writer"].reasons)
    assert any("no_tool_call" in reason for reason in verdicts["test_writer"].reasons)

    assert verdicts["manager"].certified is False


def test_evaluate_roles_fails_closed_on_missing_probe() -> None:
    transcript = ConformanceTranscript(
        base_url=normalize_base_url(_BASE_URL),
        model="tiny-coder",
        suite_version=CONFORMANCE_SUITE_VERSION,
        results=(ProbeResult(probe=PROBE_REACHABILITY, passed=True, reason="", response_hash="sha256:x"),),
    )
    (verdict,) = evaluate_roles(transcript, ["linter"])
    assert verdict.certified is False
    assert any("not_probed" in reason for reason in verdict.reasons)


def test_evaluate_roles_is_deterministic() -> None:
    transcript = _run(EndpointBehavior(tools_ok=False, context_ok=False))
    first = evaluate_roles(transcript, ["manager", "linter", "test_writer"])
    second = evaluate_roles(transcript, ["manager", "linter", "test_writer"])
    assert first == second
    assert tuple(v.role for v in first) == ("linter", "manager", "test_writer")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def test_normalize_base_url_strips_trailing_slash() -> None:
    assert normalize_base_url("http://127.0.0.1:11434/v1/") == "http://127.0.0.1:11434/v1"
    assert normalize_base_url("http://127.0.0.1:11434/v1") == "http://127.0.0.1:11434/v1"


def test_discover_default_model_reads_models_listing() -> None:
    transport = FakeTransport(EndpointBehavior(model="listed-model"))
    assert discover_default_model(base_url=_BASE_URL, transport=transport) == "listed-model"


def test_discover_default_model_returns_none_when_unavailable() -> None:
    transport = FakeTransport(EndpointBehavior(models_ok=False))
    assert discover_default_model(base_url=_BASE_URL, transport=transport) is None
