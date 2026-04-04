"""Unit tests for CacheBreakCorrelator — distributed cache break correlation."""

from __future__ import annotations

import time

from bernstein.core.prompt_caching import (
    CacheBreakCorrelator,
    CacheBreakEvent,
    CacheBreakReason,
)


def _event(
    session_id: str,
    fingerprint: str,
    reason: CacheBreakReason = CacheBreakReason.SYSTEM,
    ts: float | None = None,
) -> CacheBreakEvent:
    """Build a minimal CacheBreakEvent for tests."""
    return CacheBreakEvent(
        timestamp=ts if ts is not None else time.time(),
        reason=reason,
        old_cache_key=None,
        new_cache_key=f"key-{fingerprint}",
        estimated_token_delta=100,
        session_id=session_id,
        component_fingerprint=fingerprint,
    )


# ---------------------------------------------------------------------------
# Basic single-agent (local) behaviour
# ---------------------------------------------------------------------------


def test_single_agent_classified_as_local() -> None:
    """A break from one agent is labelled 'local', not systemic."""
    corr = CacheBreakCorrelator(min_agents_for_systemic=2)
    result = corr.add_event(_event("agent-1", "fp-abc"))
    assert not result.is_systemic
    assert result.label == "local"
    assert result.agent_ids == ["agent-1"]


def test_same_agent_repeated_breaks_stay_local() -> None:
    """Repeated breaks from the same agent don't become systemic."""
    corr = CacheBreakCorrelator(min_agents_for_systemic=2)
    corr.add_event(_event("agent-1", "fp-abc"))
    result = corr.add_event(_event("agent-1", "fp-abc"))
    assert not result.is_systemic
    # agent_ids should only count each distinct agent once
    assert result.agent_ids.count("agent-1") == 1


# ---------------------------------------------------------------------------
# Systemic detection
# ---------------------------------------------------------------------------


def test_two_agents_same_fingerprint_is_systemic() -> None:
    """Two agents breaking on the same fingerprint triggers systemic label."""
    corr = CacheBreakCorrelator(min_agents_for_systemic=2)
    corr.add_event(_event("agent-1", "fp-shared"))
    result = corr.add_event(_event("agent-2", "fp-shared"))
    assert result.is_systemic
    assert result.label == "systemic"
    assert set(result.agent_ids) == {"agent-1", "agent-2"}


def test_different_fingerprints_not_correlated() -> None:
    """Agents breaking on different fingerprints are not correlated together."""
    corr = CacheBreakCorrelator(min_agents_for_systemic=2)
    corr.add_event(_event("agent-1", "fp-A"))
    result = corr.add_event(_event("agent-2", "fp-B"))
    # fp-B has only one agent
    assert not result.is_systemic
    assert result.label == "local"


def test_three_agents_required_threshold() -> None:
    """min_agents_for_systemic=3 requires three agents before labelling systemic."""
    corr = CacheBreakCorrelator(min_agents_for_systemic=3)
    corr.add_event(_event("agent-1", "fp-x"))
    r2 = corr.add_event(_event("agent-2", "fp-x"))
    assert not r2.is_systemic  # only 2 agents so far
    r3 = corr.add_event(_event("agent-3", "fp-x"))
    assert r3.is_systemic


# ---------------------------------------------------------------------------
# Time-window eviction
# ---------------------------------------------------------------------------


def test_events_outside_window_are_evicted() -> None:
    """Events older than window_seconds are excluded from correlation."""
    now = time.time()
    corr = CacheBreakCorrelator(window_seconds=10.0, min_agents_for_systemic=2)
    # agent-1 breaks 20s ago (outside window)
    corr.add_event(_event("agent-1", "fp-old", ts=now - 20))
    # agent-2 breaks now (inside window)
    result = corr.add_event(_event("agent-2", "fp-old", ts=now))
    # After eviction, only agent-2's event should remain → not systemic
    assert not result.is_systemic


def test_events_within_window_stay_correlated() -> None:
    """Events within window_seconds are retained and correlated."""
    now = time.time()
    corr = CacheBreakCorrelator(window_seconds=60.0, min_agents_for_systemic=2)
    corr.add_event(_event("agent-1", "fp-ok", ts=now - 30))
    result = corr.add_event(_event("agent-2", "fp-ok", ts=now))
    assert result.is_systemic


# ---------------------------------------------------------------------------
# Capacity bounding
# ---------------------------------------------------------------------------


def test_max_fingerprints_evicts_oldest() -> None:
    """Exceeding max_fingerprints evicts the oldest fingerprint group."""
    corr = CacheBreakCorrelator(max_fingerprints=3)
    for i in range(4):
        corr.add_event(_event(f"agent-{i}", f"fp-{i}"))
    # Only 3 fingerprints should remain in the buffer
    correlations = corr.get_correlations()
    assert len(correlations) <= 3


# ---------------------------------------------------------------------------
# Batch correlation
# ---------------------------------------------------------------------------


def test_correlate_batch_systemic() -> None:
    """correlate_batch detects systemic break across two agents."""
    now = time.time()
    events = [
        _event("agent-1", "fp-batch", ts=now - 5),
        _event("agent-2", "fp-batch", ts=now),
    ]
    corr = CacheBreakCorrelator(min_agents_for_systemic=2)
    results = corr.correlate_batch(events)
    systemic = [r for r in results if r.is_systemic]
    assert len(systemic) == 1
    assert systemic[0].fingerprint == "fp-batch"


def test_correlate_batch_local_only() -> None:
    """correlate_batch leaves local breaks as local."""
    now = time.time()
    events = [
        _event("agent-1", "fp-A", ts=now - 5),
        _event("agent-2", "fp-B", ts=now),
    ]
    corr = CacheBreakCorrelator(min_agents_for_systemic=2)
    results = corr.correlate_batch(events)
    assert all(not r.is_systemic for r in results)


# ---------------------------------------------------------------------------
# component_fingerprint field on CacheBreakEvent
# ---------------------------------------------------------------------------


def test_cache_break_event_serializes_fingerprint() -> None:
    """component_fingerprint is round-tripped through to_dict/from_dict."""
    ev = _event("s1", "fp-serial")
    d = ev.to_dict()
    assert d["component_fingerprint"] == "fp-serial"
    restored = CacheBreakEvent.from_dict(d)
    assert restored.component_fingerprint == "fp-serial"


def test_cache_break_event_missing_fingerprint_defaults_empty() -> None:
    """Deserializing an old event without component_fingerprint field defaults to ''."""
    data = {
        "timestamp": time.time(),
        "reason": "system",
        "old_cache_key": None,
        "new_cache_key": "key-xyz",
        "estimated_token_delta": 0,
        "session_id": "s1",
    }
    ev = CacheBreakEvent.from_dict(data)
    assert ev.component_fingerprint == ""
