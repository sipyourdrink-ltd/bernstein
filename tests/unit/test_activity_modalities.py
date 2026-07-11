"""Tests for non-coding modality activities (issue #2311).

The typed activity boundary is only useful if a *non-coding* agent modality runs
under it end to end. These tests prove two modalities:

* AC1 -- a research activity records a content hash per fetched page, and a
  replay reattaches identical bytes.
* AC5 -- a browser / computer-use activity records an observation hash per
  decision step that a replay can compare.

Data / ops modalities are documented as deferred follow-ups; the substrate they
build on (``ActivityResult`` + ``dispatch_activity``) is exercised here.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from bernstein.core.orchestration.activity import (
    ActivityKind,
    TerminalState,
    dispatch_activity,
    evidence_set_hash,
)
from bernstein.core.orchestration.activity_modalities import (
    BrowserActivity,
    ContentStore,
    ResearchActivity,
    replay_reattach,
)
from bernstein.core.replay.journal import EventJournal, load_events


def _journal(tmp_path: Path, run_id: str = "run-1") -> EventJournal:
    return EventJournal(run_id=run_id, sdd_dir=tmp_path / ".sdd")


# ---------------------------------------------------------------------------
# AC1 -- research: content hash per fetched page, replay reattaches bytes
# ---------------------------------------------------------------------------


def test_research_content_addresses_each_fetched_page(tmp_path: Path) -> None:
    store = ContentStore(tmp_path / "cas")
    research = ResearchActivity(store=store)
    # Fetch two pages at "fetch time"; each is content-addressed as it lands.
    o1 = research.fetch("https://example.com/a", b"<html>alpha</html>")
    o2 = research.fetch("https://example.com/b", b"<html>beta</html>")

    assert o1.content_hash != o2.content_hash
    assert o1.content_hash.startswith("sha256:")
    # The bytes are retrievable from the store by their content hash.
    assert store.get(o1.content_hash) == b"<html>alpha</html>"
    assert store.get(o2.content_hash) == b"<html>beta</html>"


def test_research_result_pins_evidence_set_of_fetched_pages(tmp_path: Path) -> None:
    store = ContentStore(tmp_path / "cas")
    research = ResearchActivity(store=store)
    research.fetch("https://example.com/a", b"alpha")
    research.fetch("https://example.com/b", b"beta")
    result = research.finish(artifact={"summary": "found 2 sources"})

    assert result.kind is ActivityKind.RESEARCH
    assert result.terminal_state is TerminalState.COMPLETED
    assert len(result.observations) == 2
    # The evidence set hash equals the hash over the fetched observations.
    assert result.evidence_set_hash == evidence_set_hash(result.observations)


def test_research_replay_reattaches_identical_bytes(tmp_path: Path) -> None:
    # Run 1: fetch pages, anchor the activity into a journal.
    store = ContentStore(tmp_path / "cas")
    research = ResearchActivity(store=store)
    research.fetch("https://example.com/a", b"<html>alpha</html>")
    research.fetch("https://example.com/b", b"<html>beta</html>")
    result = research.finish(artifact={"summary": "s"})
    journal = _journal(tmp_path)
    dispatch_activity(result, stage_id="research-0", journal=journal)

    # Replay: walk the journal, reattach the bytes from the content store by the
    # per-page content hashes. The reattached bytes are byte-identical.
    reattached = replay_reattach(journal.path, store=store, stage_id="research-0")
    assert reattached == [b"<html>alpha</html>", b"<html>beta</html>"]


def test_research_replay_detects_tampered_bytes(tmp_path: Path) -> None:
    store = ContentStore(tmp_path / "cas")
    research = ResearchActivity(store=store)
    obs = research.fetch("https://example.com/a", b"<html>alpha</html>")
    result = research.finish(artifact={"summary": "s"})
    journal = _journal(tmp_path)
    dispatch_activity(result, stage_id="r0", journal=journal)

    # Tamper the stored bytes for that content hash: replay must refuse to
    # reattach because the recomputed hash no longer matches the pinned hash.
    store.force_put(obs.content_hash, b"<html>TAMPERED</html>")
    with pytest.raises(ValueError, match="content hash mismatch"):
        replay_reattach(journal.path, store=store, stage_id="r0")


def test_research_replay_missing_bytes_raises(tmp_path: Path) -> None:
    store = ContentStore(tmp_path / "cas")
    research = ResearchActivity(store=store)
    research.fetch("https://example.com/a", b"alpha")
    result = research.finish(artifact={"summary": "s"})
    journal = _journal(tmp_path)
    dispatch_activity(result, stage_id="r0", journal=journal)

    # A fresh store (no bytes) cannot reattach.
    empty = ContentStore(tmp_path / "cas-empty")
    with pytest.raises(KeyError):
        replay_reattach(journal.path, store=empty, stage_id="r0")


def test_research_fetch_is_idempotent_on_same_bytes(tmp_path: Path) -> None:
    store = ContentStore(tmp_path / "cas")
    research = ResearchActivity(store=store)
    a = research.fetch("https://example.com/a", b"same")
    b = research.fetch("https://example.com/a-mirror", b"same")
    # Same bytes -> same content hash regardless of URL.
    assert a.content_hash == b.content_hash


# ---------------------------------------------------------------------------
# AC5 -- browser: observation hash per decision step
# ---------------------------------------------------------------------------


def test_browser_records_observation_hash_per_decision(tmp_path: Path) -> None:
    store = ContentStore(tmp_path / "cas")
    browser = BrowserActivity(store=store)
    # Each decision step records the observation (screenshot / DOM snapshot) the
    # model saw before it acted.
    d1 = browser.observe(step="click-login", snapshot=b"<dom>login-page</dom>")
    d2 = browser.observe(step="fill-form", snapshot=b"<dom>form-page</dom>")

    assert d1.content_hash != d2.content_hash
    assert d1.ref == "click-login"
    assert d2.ref == "fill-form"


def test_browser_result_orders_observations_by_decision(tmp_path: Path) -> None:
    store = ContentStore(tmp_path / "cas")
    browser = BrowserActivity(store=store)
    browser.observe(step="s1", snapshot=b"a")
    browser.observe(step="s2", snapshot=b"b")
    browser.observe(step="s3", snapshot=b"c")
    result = browser.finish(artifact={"outcome": "logged-in"})

    assert result.kind is ActivityKind.BROWSER
    assert [o.ref for o in result.observations] == ["s1", "s2", "s3"]
    assert len(result.observations) == 3


def test_browser_replay_compares_observation_hashes(tmp_path: Path) -> None:
    store = ContentStore(tmp_path / "cas")
    browser = BrowserActivity(store=store)
    browser.observe(step="s1", snapshot=b"snap-1")
    browser.observe(step="s2", snapshot=b"snap-2")
    result = browser.finish(artifact={"outcome": "done"})
    journal = _journal(tmp_path)
    dispatch_activity(result, stage_id="browse-0", journal=journal)

    # Replay reattaches the per-decision snapshots and confirms their hashes.
    reattached = replay_reattach(journal.path, store=store, stage_id="browse-0")
    assert reattached == [b"snap-1", b"snap-2"]


def test_browser_replay_detects_divergent_observation(tmp_path: Path) -> None:
    store = ContentStore(tmp_path / "cas")
    browser = BrowserActivity(store=store)
    obs = browser.observe(step="s1", snapshot=b"snap-1")
    result = browser.finish(artifact={"outcome": "done"})
    journal = _journal(tmp_path)
    dispatch_activity(result, stage_id="b0", journal=journal)

    store.force_put(obs.content_hash, b"different-snapshot")
    with pytest.raises(ValueError, match="content hash mismatch"):
        replay_reattach(journal.path, store=store, stage_id="b0")


# ---------------------------------------------------------------------------
# cross-modality: the journal event shape is identical across modalities
# ---------------------------------------------------------------------------


def test_activity_event_shape_is_modality_agnostic(tmp_path: Path) -> None:
    store = ContentStore(tmp_path / "cas")
    research = ResearchActivity(store=store)
    research.fetch("https://a", b"alpha")
    r_result = research.finish(artifact={"summary": "s"})

    browser = BrowserActivity(store=store)
    browser.observe(step="s1", snapshot=b"snap")
    b_result = browser.finish(artifact={"outcome": "o"})

    journal = _journal(tmp_path)
    dispatch_activity(r_result, stage_id="r0", journal=journal)
    dispatch_activity(b_result, stage_id="b0", journal=journal)

    rows = load_events(journal.path)
    # Both modalities journal the same event type and the same key set, so the
    # scheduler dispatches and journals them identically (the epic's core goal).
    assert {r["event"] for r in rows} == {"activity.result"}
    assert set(rows[0]) == set(rows[1])
    assert rows[0]["kind"] == "research"
    assert rows[1]["kind"] == "browser"
