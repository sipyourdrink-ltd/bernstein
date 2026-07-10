"""Per-role provider fallback chains and deterministic failover (issue #2355).

Covers the availability-policy substrate:

- config parsing with conformance floors (a below-floor fallback is rejected
  at validation time),
- probe results as the sole routing input (same recorded probe set ->
  byte-identical routing decision, hash included),
- deterministic failover selection (first healthy chain element wins),
- the probe cache TTL contract,
- the failover drill report (broken chain -> not ok).
"""

from __future__ import annotations

import pytest

from bernstein.core.routing.provider_availability import (
    REASON_FAILOVER,
    REASON_NO_HEALTHY_PROVIDER,
    REASON_PRIMARY_HEALTHY,
    AvailabilityPolicyError,
    ChainElement,
    ProbeCache,
    ProbeResult,
    RoleAvailabilityPolicy,
    binary_path_probe,
    decide_route,
    disabled_probe,
    parse_provider_availability,
    resolve_route,
    run_failover_drill,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _policy(*elements: tuple[str, str, str], role: str = "developer", floor: str = "basic") -> RoleAvailabilityPolicy:
    chain = tuple(ChainElement(adapter=a, model=m, conformance=c) for a, m, c in elements)
    return RoleAvailabilityPolicy(role=role, conformance_floor=floor, chain=chain)


def _probe(adapter: str, *, healthy: bool, kind: str = "test") -> ProbeResult:
    return ProbeResult(adapter=adapter, healthy=healthy, probe_kind=kind, detail="", checked_at=0.0)


_SECTION = {
    "probe_ttl_minutes": 7,
    "probes_enabled": True,
    "roles": {
        "developer": {
            "conformance_floor": "advanced",
            "chain": [
                {"adapter": "claude", "model": "opus", "conformance": "expert"},
                {"adapter": "codex", "model": "gpt-5.2", "conformance": "advanced"},
            ],
        },
    },
}


# ---------------------------------------------------------------------------
# Config parsing + conformance floor (AC: below-floor fallback rejected)
# ---------------------------------------------------------------------------


def test_parse_provider_availability_roundtrip() -> None:
    config = parse_provider_availability(_SECTION)
    assert config.probe_ttl_minutes == 7
    assert config.probes_enabled is True
    policy = config.policies["developer"]
    assert policy.conformance_floor == "advanced"
    assert policy.chain[0] == ChainElement(adapter="claude", model="opus", conformance="expert")
    assert policy.chain[1].adapter == "codex"


def test_parse_rejects_fallback_below_conformance_floor() -> None:
    section = {
        "roles": {
            "developer": {
                "conformance_floor": "advanced",
                "chain": [
                    {"adapter": "claude", "model": "opus", "conformance": "expert"},
                    {"adapter": "qwen", "model": "qwen3-coder", "conformance": "basic"},
                ],
            },
        },
    }
    with pytest.raises(AvailabilityPolicyError) as excinfo:
        parse_provider_availability(section)
    message = str(excinfo.value)
    assert "developer" in message
    assert "position 1" in message
    assert "basic" in message
    assert "advanced" in message


def test_parse_rejects_empty_chain() -> None:
    section = {"roles": {"developer": {"conformance_floor": "basic", "chain": []}}}
    with pytest.raises(AvailabilityPolicyError):
        parse_provider_availability(section)


def test_parse_rejects_unknown_conformance_level() -> None:
    section = {
        "roles": {
            "developer": {
                "conformance_floor": "galactic",
                "chain": [{"adapter": "claude", "model": "opus", "conformance": "expert"}],
            },
        },
    }
    with pytest.raises(AvailabilityPolicyError):
        parse_provider_availability(section)


def test_parse_rejects_non_mapping_section() -> None:
    with pytest.raises(AvailabilityPolicyError):
        parse_provider_availability(["not", "a", "mapping"])  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Deterministic selection (AC: replay reproduces routing decisions)
# ---------------------------------------------------------------------------


def test_decide_route_picks_primary_when_healthy() -> None:
    policy = _policy(("claude", "opus", "expert"), ("codex", "gpt-5.2", "advanced"))
    probes = (_probe("claude", healthy=True), _probe("codex", healthy=True))
    decision = decide_route(policy, probes)
    assert decision.chosen_index == 0
    assert decision.reason == REASON_PRIMARY_HEALTHY
    assert decision.chosen is not None
    assert decision.chosen.adapter == "claude"


def test_decide_route_fails_over_to_first_healthy_element() -> None:
    policy = _policy(
        ("claude", "opus", "expert"),
        ("codex", "gpt-5.2", "advanced"),
        ("gemini", "gemini-3-pro", "advanced"),
    )
    probes = (
        _probe("claude", healthy=False),
        _probe("codex", healthy=False),
        _probe("gemini", healthy=True),
    )
    decision = decide_route(policy, probes)
    assert decision.chosen_index == 2
    assert decision.reason == REASON_FAILOVER
    assert decision.chosen is not None
    assert decision.chosen.adapter == "gemini"


def test_decide_route_reports_no_healthy_provider() -> None:
    policy = _policy(("claude", "opus", "expert"), ("codex", "gpt-5.2", "advanced"))
    probes = (_probe("claude", healthy=False), _probe("codex", healthy=False))
    decision = decide_route(policy, probes)
    assert decision.chosen_index == -1
    assert decision.reason == REASON_NO_HEALTHY_PROVIDER
    assert decision.chosen is None


def test_decide_route_rejects_probe_chain_length_mismatch() -> None:
    policy = _policy(("claude", "opus", "expert"), ("codex", "gpt-5.2", "advanced"))
    with pytest.raises(AvailabilityPolicyError):
        decide_route(policy, (_probe("claude", healthy=True),))


def test_same_recorded_probes_reproduce_identical_decision() -> None:
    """AC: replay reproduces routing decisions given the same recorded probe results."""
    policy = _policy(("claude", "opus", "expert"), ("codex", "gpt-5.2", "advanced"))
    probes = (_probe("claude", healthy=False), _probe("codex", healthy=True))

    first = decide_route(policy, probes)
    replayed = decide_route(policy, probes)

    assert first.projection() == replayed.projection()
    assert first.decision_hash == replayed.decision_hash
    assert first.decision_hash.startswith("sha256:")


def test_decision_hash_ignores_probe_timestamps_and_detail() -> None:
    """The hash covers the decision-determining projection only."""
    policy = _policy(("claude", "opus", "expert"), ("codex", "gpt-5.2", "advanced"))
    recorded = (
        ProbeResult(adapter="claude", healthy=False, probe_kind="test", detail="run A", checked_at=1.0),
        ProbeResult(adapter="codex", healthy=True, probe_kind="test", detail="run A", checked_at=2.0),
    )
    replayed = (
        ProbeResult(adapter="claude", healthy=False, probe_kind="test", detail="run B", checked_at=99.0),
        ProbeResult(adapter="codex", healthy=True, probe_kind="test", detail="run B", checked_at=100.0),
    )
    assert decide_route(policy, recorded).decision_hash == decide_route(policy, replayed).decision_hash


def test_different_probe_outcomes_change_decision_hash() -> None:
    policy = _policy(("claude", "opus", "expert"), ("codex", "gpt-5.2", "advanced"))
    healthy = (_probe("claude", healthy=True), _probe("codex", healthy=True))
    blackholed = (_probe("claude", healthy=False), _probe("codex", healthy=True))
    assert decide_route(policy, healthy).decision_hash != decide_route(policy, blackholed).decision_hash


# ---------------------------------------------------------------------------
# Probes + cache TTL
# ---------------------------------------------------------------------------


def test_probe_cache_serves_cached_result_within_ttl() -> None:
    calls: list[str] = []

    def prober(element: ChainElement) -> ProbeResult:
        calls.append(element.adapter)
        return _probe(element.adapter, healthy=True)

    cache = ProbeCache(ttl_seconds=300.0)
    element = ChainElement(adapter="claude", model="opus", conformance="expert")
    cache.get_or_probe(element, prober, now=1000.0)
    cache.get_or_probe(element, prober, now=1100.0)
    assert calls == ["claude"]


def test_probe_cache_reprobes_after_ttl_expiry() -> None:
    calls: list[str] = []

    def prober(element: ChainElement) -> ProbeResult:
        calls.append(element.adapter)
        return _probe(element.adapter, healthy=True)

    cache = ProbeCache(ttl_seconds=300.0)
    element = ChainElement(adapter="claude", model="opus", conformance="expert")
    cache.get_or_probe(element, prober, now=1000.0)
    cache.get_or_probe(element, prober, now=1301.0)
    assert calls == ["claude", "claude"]


def test_binary_path_probe_reports_missing_binary_unhealthy() -> None:
    element = ChainElement(adapter="definitely-not-a-real-binary-2355", model="x", conformance="basic")
    result = binary_path_probe(element)
    assert result.healthy is False
    assert result.probe_kind == "binary_path"


def test_binary_path_probe_reports_present_binary_healthy() -> None:
    # git is a hard prerequisite of the project, so it is always on PATH in CI.
    element = ChainElement(adapter="git", model="n/a", conformance="basic")
    result = binary_path_probe(element)
    assert result.healthy is True


def test_disabled_probe_is_always_healthy() -> None:
    element = ChainElement(adapter="anything", model="x", conformance="basic")
    result = disabled_probe(element)
    assert result.healthy is True
    assert result.probe_kind == "disabled"


def test_resolve_route_with_probes_disabled_selects_primary() -> None:
    policy = _policy(("missing-binary-a", "x", "basic"), ("missing-binary-b", "y", "basic"))
    cache = ProbeCache(ttl_seconds=60.0)
    decision = resolve_route(policy, cache=cache, prober=binary_path_probe, probes_enabled=False)
    assert decision.chosen_index == 0
    assert all(p.probe_kind == "disabled" for p in decision.probes)


# ---------------------------------------------------------------------------
# Failover drill (AC: broken chain -> non-zero, all healthy -> pass)
# ---------------------------------------------------------------------------


def test_drill_passes_when_every_chain_element_is_healthy() -> None:
    config = parse_provider_availability(_SECTION)
    report = run_failover_drill(config, prober=lambda e: _probe(e.adapter, healthy=True))
    assert report.ok is True
    assert report.broken_roles == ()
    # Every declared fallback path is exercised: one row per chain position.
    assert len(report.elements) == 2
    assert [row.position for row in report.elements] == [0, 1]


def test_drill_flags_broken_chain_element() -> None:
    config = parse_provider_availability(_SECTION)

    def prober(element: ChainElement) -> ProbeResult:
        return _probe(element.adapter, healthy=element.adapter != "codex")

    report = run_failover_drill(config, prober=prober)
    assert report.ok is False
    assert report.broken_roles == ("developer",)
    broken = [row for row in report.elements if not row.healthy]
    assert len(broken) == 1
    assert broken[0].adapter == "codex"
    assert broken[0].position == 1


def test_drill_rows_carry_deterministic_decision_hashes() -> None:
    """Each drill row records the routing decision for the simulated outage prefix."""
    config = parse_provider_availability(_SECTION)
    report_a = run_failover_drill(config, prober=lambda e: _probe(e.adapter, healthy=True))
    report_b = run_failover_drill(config, prober=lambda e: _probe(e.adapter, healthy=True))
    assert [row.decision_hash for row in report_a.elements] == [row.decision_hash for row in report_b.elements]
    assert all(row.decision_hash.startswith("sha256:") for row in report_a.elements)
