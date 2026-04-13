"""Tests that CachingAdapter wires cache break events into the shared correlator."""

from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import MagicMock

from bernstein.core.prompt_caching import (
    CacheBreakCorrelator,
    CacheBreakEvent,
    CacheBreakReason,
)

from bernstein.adapters.base import CLIAdapter, SpawnResult
from bernstein.adapters.caching_adapter import CachingAdapter


def _make_event(session_id: str, fingerprint: str, ts: float | None = None) -> CacheBreakEvent:
    return CacheBreakEvent(
        timestamp=ts if ts is not None else time.time(),
        reason=CacheBreakReason.SYSTEM,
        old_cache_key=None,
        new_cache_key=f"key-{fingerprint}",
        estimated_token_delta=50,
        session_id=session_id,
        component_fingerprint=fingerprint,
    )


def _adapter(tmp_path: Path, correlator: CacheBreakCorrelator) -> CachingAdapter:
    inner = MagicMock(spec=CLIAdapter)
    inner.name.return_value = "mock"
    inner.spawn.return_value = SpawnResult(pid=1, log_path=tmp_path / "x.log")
    inner.is_rate_limited.return_value = False
    return CachingAdapter(inner, tmp_path, correlator=correlator)


# ---------------------------------------------------------------------------
# Wiring: adapter feeds correlator
# ---------------------------------------------------------------------------


def test_record_break_populates_correlator(tmp_path: Path) -> None:
    """_record_cache_break adds the event to the injected correlator."""
    corr = CacheBreakCorrelator(min_agents_for_systemic=2)
    adapter = _adapter(tmp_path, corr)

    adapter._record_cache_break(_make_event("agent-1", "fp-xyz"))  # pyright: ignore[reportPrivateUsage]

    correlations = corr.get_correlations()
    assert len(correlations) == 1
    assert correlations[0].fingerprint == "fp-xyz"
    assert correlations[0].label == "local"


def test_record_break_also_writes_jsonl(tmp_path: Path) -> None:
    """_record_cache_break writes the event line to cache_breaks.jsonl."""
    corr = CacheBreakCorrelator()
    adapter = _adapter(tmp_path, corr)

    adapter._record_cache_break(_make_event("agent-1", "fp-abc"))  # pyright: ignore[reportPrivateUsage]

    jsonl = tmp_path / ".sdd" / "metrics" / "cache_breaks.jsonl"
    assert jsonl.exists()
    lines = [ln for ln in jsonl.read_text().splitlines() if ln.strip()]
    assert len(lines) == 1
    import json

    data = json.loads(lines[0])
    assert data["component_fingerprint"] == "fp-abc"


# ---------------------------------------------------------------------------
# Systemic detection through adapter
# ---------------------------------------------------------------------------


def test_two_agents_same_fingerprint_is_systemic_via_adapter(tmp_path: Path) -> None:
    """Two adapters sharing a correlator detect a systemic break."""
    corr = CacheBreakCorrelator(min_agents_for_systemic=2)
    adapter1 = _adapter(tmp_path, corr)
    adapter2 = _adapter(tmp_path, corr)

    adapter1._record_cache_break(_make_event("agent-1", "fp-shared"))  # pyright: ignore[reportPrivateUsage]
    adapter2._record_cache_break(_make_event("agent-2", "fp-shared"))  # pyright: ignore[reportPrivateUsage]

    correlations = corr.get_correlations()
    assert len(correlations) == 1
    assert correlations[0].is_systemic
    assert correlations[0].label == "systemic"
    assert set(correlations[0].agent_ids) == {"agent-1", "agent-2"}


def test_different_fingerprints_stay_local_via_adapter(tmp_path: Path) -> None:
    """Different fingerprints are not promoted to systemic."""
    corr = CacheBreakCorrelator(min_agents_for_systemic=2)
    adapter1 = _adapter(tmp_path, corr)
    adapter2 = _adapter(tmp_path, corr)

    adapter1._record_cache_break(_make_event("agent-1", "fp-A"))  # pyright: ignore[reportPrivateUsage]
    adapter2._record_cache_break(_make_event("agent-2", "fp-B"))  # pyright: ignore[reportPrivateUsage]

    correlations = corr.get_correlations()
    assert all(not c.is_systemic for c in correlations)


def test_same_agent_repeated_breaks_stay_local_via_adapter(tmp_path: Path) -> None:
    """Repeated breaks from the same agent do not trigger systemic label."""
    corr = CacheBreakCorrelator(min_agents_for_systemic=2)
    adapter = _adapter(tmp_path, corr)

    adapter._record_cache_break(_make_event("agent-1", "fp-rep"))  # pyright: ignore[reportPrivateUsage]
    adapter._record_cache_break(_make_event("agent-1", "fp-rep"))  # pyright: ignore[reportPrivateUsage]

    correlations = corr.get_correlations()
    assert len(correlations) == 1
    assert not correlations[0].is_systemic


# ---------------------------------------------------------------------------
# Structured log fields
# ---------------------------------------------------------------------------


def test_structured_log_emitted_for_systemic_break(tmp_path: Path) -> None:
    """Logger.warning is emitted with break_label and fingerprint extras for systemic breaks."""
    import logging

    corr = CacheBreakCorrelator(min_agents_for_systemic=2)

    records: list[logging.LogRecord] = []

    class Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    handler = Capture()
    logging.getLogger("bernstein.core.tokens.prompt_caching").addHandler(handler)
    try:
        corr.add_event(_make_event("a1", "fp-sys"))
        corr.add_event(_make_event("a2", "fp-sys"))
    finally:
        logging.getLogger("bernstein.core.tokens.prompt_caching").removeHandler(handler)

    warnings = [r for r in records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert getattr(warnings[0], "break_label", None) == "systemic"
    assert getattr(warnings[0], "agent_count", None) == 2
