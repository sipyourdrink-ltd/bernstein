"""No opt-in gap: one production event per spine write, every time (#2559, AC8).

``record_artifact_write`` is the single write boundary for per-artifact
provenance. The point of emitting from *there* rather than from each caller is
that there is nothing to forget: an adapter cannot record provenance and skip
the announcement, because the two are one call.

These tests pin the property from both sides:

* the conformance side -- for every adapter in the registry and for every
  in-process caller of the boundary, ``len(events) == len(spine entries)`` and
  the hashes line up one for one;
* the isolation side (AC7) -- the emit path is fail-open, so a broken journal,
  a broken bus or a hostile subscriber cannot turn a successful artifact write
  into a failed one.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bernstein.adapters.base import (
    post_write_lineage_hook,
    record_artifact_write,
    set_artifact_event_publisher,
)
from bernstein.adapters.registry import selectable_adapter_names
from bernstein.core.lineage.artifact_events import (
    load_production_events,
    replay_production_events,
)
from bernstein.core.lineage.spine import LineageSpine

_KEY = b"c" * 32
_RUN = "run-conformance"


@pytest.fixture(autouse=True)
def _no_ambient_publisher() -> object:
    """Keep a publisher installed by another test out of these assertions."""
    set_artifact_event_publisher(None)
    yield
    set_artifact_event_publisher(None)


def _root(tmp_path: Path) -> Path:
    return tmp_path / ".sdd" / "lineage"


def _entries(tmp_path: Path, run_id: str = _RUN) -> list[object]:
    return list(LineageSpine(_root(tmp_path), run_id=run_id, hmac_key=_KEY).iter_entries())


def _write(tmp_path: Path, *, path: str, actor: str, run_id: str = _RUN, ts: int = 1) -> str | None:
    return record_artifact_write(
        artifact_path=path,
        content=f"bytes-for-{path}".encode(),
        actor=actor,
        step_id="step",
        model="model-x",
        lineage_root=_root(tmp_path),
        run_id=run_id,
        hmac_key=_KEY,
        timestamp=ts,
    )


# ---------------------------------------------------------------------------
# One event per write, across adapters
# ---------------------------------------------------------------------------


def test_every_registered_adapter_emits_exactly_one_event_per_write(tmp_path: Path) -> None:
    """The boundary is shared, so no adapter can be the one that forgets."""
    names = sorted(selectable_adapter_names())
    assert names, "adapter registry should not be empty"

    for i, name in enumerate(names):
        _write(tmp_path, path=f"out/{name}.txt", actor=name, ts=i)

    events = load_production_events(_root(tmp_path), run_id=_RUN)
    entries = _entries(tmp_path)
    assert len(events) == len(entries) == len(names)
    assert [e.actor for e in events] == names
    assert [e.entry_hash for e in events] == [entry.entry_hash for entry in entries]  # type: ignore[attr-defined]


def test_one_write_emits_exactly_one_event(tmp_path: Path) -> None:
    entry_hash = _write(tmp_path, path="dist/pkg.whl", actor="claude")
    events = load_production_events(_root(tmp_path), run_id=_RUN)
    assert len(events) == 1
    assert events[0].entry_hash == entry_hash


def test_repeated_writes_of_the_same_key_each_emit(tmp_path: Path) -> None:
    """Idempotence is not assumed: a second write is a second production."""
    for i in range(3):
        _write(tmp_path, path="dist/pkg.whl", actor="claude", ts=i)
    assert len(load_production_events(_root(tmp_path), run_id=_RUN)) == 3


def test_external_uri_writes_emit_like_repo_path_writes(tmp_path: Path) -> None:
    for i, key in enumerate(
        [
            "pr://github.com/acme/widget/2559",
            "pkg://pypi/bernstein/3.9.0",
            "deploy://prod/docs-site",
            "doc://example.test/lineage/artifacts",
            "src/bernstein/core/lineage/spine.py",
        ]
    ):
        _write(tmp_path, path=key, actor="claude", ts=i)
    events = load_production_events(_root(tmp_path), run_id=_RUN)
    assert len(events) == 5
    assert [e.uri for e in events] == [
        "pr://github.com/acme/widget/2559",
        "pkg://pypi/bernstein/3.9.0",
        "deploy://prod/docs-site",
        "doc://example.test/lineage/artifacts",
        "src/bernstein/core/lineage/spine.py",
    ]


def test_the_deprecated_v1_shim_emits_too(tmp_path: Path) -> None:
    """The shim routes through the boundary, so it inherits the guarantee."""
    from bernstein.core.lineage.identity import AgentCard, generate_keypair

    private_pem, public_pem = generate_keypair()
    card = AgentCard(agent_id="agent-1", public_key_pem=public_pem, kid="kid-1")
    post_write_lineage_hook(
        artefact_path="docs/report.md",
        new_content=b"report",
        agent_id="agent-1",
        agent_card=card,
        private_key_pem=private_pem,
        tool_call_id="call-1",
        span_id="span-1",
        lineage_root=_root(tmp_path),
        operator_hmac_key=_KEY,
        run_id=_RUN,
    )
    assert len(load_production_events(_root(tmp_path), run_id=_RUN)) == 1


def test_a_rejected_write_emits_nothing(tmp_path: Path) -> None:
    """No spine entry, no event: the two stay in lockstep on the refusal path."""
    with pytest.raises(ValueError, match="path traversal"):
        _write(tmp_path, path="../etc/passwd", actor="claude")
    assert load_production_events(_root(tmp_path), run_id=_RUN) == []


def test_the_disabled_gate_records_and_emits_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BERNSTEIN_LINEAGE_ENABLED", "0")
    assert _write(tmp_path, path="dist/pkg.whl", actor="claude") is None
    assert load_production_events(_root(tmp_path), run_id=_RUN) == []


def test_events_and_entries_stay_paired_across_runs(tmp_path: Path) -> None:
    _write(tmp_path, path="a.txt", actor="claude", run_id="run-a", ts=1)
    _write(tmp_path, path="b.txt", actor="codex", run_id="run-b", ts=2)
    for run_id, uri in (("run-a", "a.txt"), ("run-b", "b.txt")):
        events = load_production_events(_root(tmp_path), run_id=run_id)
        assert [e.uri for e in events] == [uri]
        assert len(_entries(tmp_path, run_id)) == 1


def test_the_journaled_set_equals_the_replayed_set(tmp_path: Path) -> None:
    """The conformance property restated as replay equality."""
    for i, name in enumerate(sorted(selectable_adapter_names())[:8]):
        _write(tmp_path, path=f"out/{name}.txt", actor=name, ts=i)
    journaled = load_production_events(_root(tmp_path), run_id=_RUN)
    replayed = replay_production_events(_root(tmp_path), run_id=_RUN, hmac_key=_KEY)
    assert [e.canonical_bytes() for e in journaled] == [e.canonical_bytes() for e in replayed]


# ---------------------------------------------------------------------------
# In-process callers of the boundary
# ---------------------------------------------------------------------------


def test_journal_seal_write_emits_one_event(tmp_path: Path) -> None:
    from bernstein.core.replay.journal import EventJournal, seal_journal_into_spine

    sdd = tmp_path / ".sdd"
    journal = EventJournal("run-seal", sdd)
    journal.record("step.started", step="one")
    seal_journal_into_spine(
        journal=journal,
        lineage_root=_root(tmp_path),
        hmac_key=_KEY,
        actor="orchestrator",
    )
    events = load_production_events(_root(tmp_path), run_id="run-seal")
    assert len(events) == 1
    assert events[0].is_journal_seal


def test_mcp_trace_context_write_emits_one_event(tmp_path: Path) -> None:
    from bernstein.core.protocols.mcp.tasks_extension import (
        TraceContext,
        record_trace_context_into_lineage,
    )

    trace = TraceContext(
        trace_id="0af7651916cd43dd8448eb211c80319c",
        parent_id="b7ad6b7169203331",
        trace_flags="01",
    )
    record_trace_context_into_lineage(
        trace=trace,
        artifact_path="docs/report.md",
        content=b"report",
        actor="mcp-host",
        run_id="run-mcp",
        lineage_root=_root(tmp_path),
        hmac_key=_KEY,
        timestamp=1,
    )
    events = load_production_events(_root(tmp_path), run_id="run-mcp")
    assert len(events) == 1
    assert events[0].uri == "docs/report.md"


# ---------------------------------------------------------------------------
# Failure-domain isolation (AC7)
# ---------------------------------------------------------------------------


def test_a_journal_write_failure_does_not_fail_the_artifact_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _boom(*_a: object, **_k: object) -> None:
        raise OSError("read-only file system")

    monkeypatch.setattr("bernstein.core.lineage.artifact_events.append_production_event", _boom)
    entry_hash = _write(tmp_path, path="dist/pkg.whl", actor="claude")

    # The write succeeded and the provenance is durable ...
    assert entry_hash is not None
    assert len(_entries(tmp_path)) == 1
    # ... the journal simply lost a row, which replay rebuilds from the spine.
    assert load_production_events(_root(tmp_path), run_id=_RUN) == []
    replayed = replay_production_events(_root(tmp_path), run_id=_RUN, hmac_key=_KEY)
    assert [e.entry_hash for e in replayed] == [entry_hash]


def test_a_hostile_subscriber_does_not_fail_the_artifact_write(tmp_path: Path) -> None:
    def _hostile(_event: object) -> None:
        raise RuntimeError("subscriber raised")

    set_artifact_event_publisher(_hostile)
    entry_hash = _write(tmp_path, path="dist/pkg.whl", actor="claude")
    assert entry_hash is not None
    # Journaling and publishing fail apart: the row is still on disk.
    assert len(load_production_events(_root(tmp_path), run_id=_RUN)) == 1


def test_a_subscriber_that_hangs_up_leaves_the_chain_intact(tmp_path: Path) -> None:
    delivered: list[str] = []

    def _flaky(event: object) -> None:
        uri = getattr(event, "uri", "")
        if uri.endswith("2.txt"):
            raise ConnectionResetError("bus went away")
        delivered.append(uri)

    set_artifact_event_publisher(_flaky)
    for i in range(4):
        _write(tmp_path, path=f"out/{i}.txt", actor="claude", ts=i)

    assert delivered == ["out/0.txt", "out/1.txt", "out/3.txt"]
    # Every write still landed, and every one is journaled.
    assert len(_entries(tmp_path)) == 4
    assert len(load_production_events(_root(tmp_path), run_id=_RUN)) == 4


def test_the_publisher_receives_the_journaled_payload_verbatim(tmp_path: Path) -> None:
    seen: list[dict[str, object]] = []
    set_artifact_event_publisher(lambda event: seen.append(event.to_payload()))
    _write(tmp_path, path="dist/pkg.whl", actor="claude")

    journal_path = _root(tmp_path) / _RUN / "artifact-events.jsonl"
    assert seen == [json.loads(journal_path.read_bytes().strip())]
