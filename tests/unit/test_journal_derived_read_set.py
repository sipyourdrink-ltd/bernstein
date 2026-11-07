"""The task receipt's read side comes from the journal, never from the task.

Slice 1 of the stale-assumption arc. The receipt already carried a write
side (``tasks[].declared_paths`` -- what a task *said* it owns) and nothing
about what the task actually read. Two tasks can then merge with zero
textual overlap while one relied on a function the other rewrote.

The property pinned throughout is provenance, not coverage: a read set is a
derivation from the Merkle-chained journal, and there is no route by which a
task's own account of what it read can reach the receipt. That matters
because the failure this record exists to catch is precisely a task being
wrong about its own assumptions -- a self-reported read set would be worth
nothing against it.

The intersection check, the re-verify/block policy and ``merge explain``
are slice 2 and are deliberately absent here.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import bernstein.core.knowledge.ast_symbol_graph as semantic_graph
from bernstein.core.knowledge.ast_symbol_graph import (
    build_semantic_graph,
    graph_digest,
    graph_document,
)
from bernstein.core.knowledge.code_graph import (
    ATTRIBUTION_PROVEN,
    SemanticCodeGraph,
    TaskNodeSet,
    attribute_task,
)
from bernstein.core.parallel_admission import (
    RECEIPT_CONSISTENT_ONLY,
    RECEIPT_DIVERGED,
    RECEIPT_VERIFIED,
    build_admission_receipt,
    verify_admission_receipt,
)
from bernstein.core.replay.journal import EventJournal
from bernstein.core.replay.read_paths import (
    ReadPathDerivationError,
    TaskReadSet,
    derive_task_read_set,
)
from bernstein.core.streaming_merge import IncrementalChunk, with_read_set

_DIGEST = "sha256:abc"

#: One trivially attributable module, enough for a real semantic graph.
_ALPHA_SRC = """def a() -> int:
    return 1
"""


def _journal(tmp_path: Path, rows: list[dict[str, object]], run_id: str = "read-set-run") -> Path:
    """Append ``rows`` into a fresh Merkle-chained journal; return its path.

    Mirrors the builder in ``tests/unit/core/replay/test_read_paths.py`` so
    both suites exercise the same real journal rather than a stand-in.
    """
    journal = EventJournal(run_id, tmp_path / ".sdd")
    for row in rows:
        event = str(row["event"])
        journal.record(event, **{k: v for k, v in row.items() if k != "event"})
    return journal.path


def _task(task_id: str, declared_paths: tuple[str, ...] = ()) -> TaskNodeSet:
    """A proven attribution whose ``declared_paths`` are the task's claim."""
    symbols = (f"{task_id}.py::sym",)
    return TaskNodeSet(
        task_id=task_id,
        declared_paths=declared_paths,
        seed_symbols=symbols,
        neighborhood=symbols,
        depth=1,
        verdict=ATTRIBUTION_PROVEN,
        reasons=(),
    )


# ---------------------------------------------------------------------------
# Provenance: the journal is the only authority
# ---------------------------------------------------------------------------


def test_read_set_comes_from_the_journal_not_the_agent(tmp_path: Path) -> None:
    """The receipt records what the journal saw, not what the task claimed.

    This is the assertion the whole slice exists for. The task declares it
    worked on ``src/beta.py``; its journal says it read ``src/alpha.py``.
    A receipt that believed the task would carry ``beta`` and would be
    useless for catching a task wrong about its own assumptions.
    """
    journal = _journal(tmp_path, [{"event": "read", "path": "src/alpha.py"}])
    claiming_task = _task("t1", declared_paths=("src/beta.py",))

    receipt = build_admission_receipt(
        _DIGEST,
        [claiming_task],
        [derive_task_read_set("t1", journal, tmp_path)],
    )

    entry = receipt["read_sets"][0]  # type: ignore[index]
    assert entry["read_paths"] == ["src/alpha.py"]
    assert "src/beta.py" not in entry["read_paths"]
    # The claim is not erased -- it stays visible, recorded as a claim.
    assert receipt["tasks"][0]["declared_paths"] == ["src/beta.py"]  # type: ignore[index]


def test_the_claim_cannot_influence_the_projected_read_set(tmp_path: Path) -> None:
    """Two tasks claiming opposite things project one identical read side.

    The claim lives in ``declared_paths`` and the read set is derived, so
    varying the claim while holding the journal fixed must not move the
    projection by one byte.
    """
    journal = _journal(tmp_path, [{"event": "read", "path": "src/alpha.py"}])
    derived = derive_task_read_set("t1", journal, tmp_path)

    honest = build_admission_receipt(_DIGEST, [_task("t1", ("src/alpha.py",))], [derived])
    lying = build_admission_receipt(_DIGEST, [_task("t1", ("src/nowhere.py",))], [derived])

    assert honest["read_sets"] == lying["read_sets"]
    assert honest["read_sets"][0]["read_paths"] == ["src/alpha.py"]  # type: ignore[index]
    # ...while the claim itself is still recorded, and still differs.
    assert honest["tasks"][0]["declared_paths"] != lying["tasks"][0]["declared_paths"]  # type: ignore[index]


def test_derivation_refuses_a_tampered_journal_instead_of_shrinking(tmp_path: Path) -> None:
    """A broken chain refuses; it never yields a smaller, admissible set.

    Inherited from ``derive_read_paths``, and pinned here because a silent
    partial set is exactly how this record would stop being load-bearing.
    """
    journal = _journal(tmp_path, [{"event": "read", "path": "src/alpha.py"}])
    raw = journal.read_bytes()
    journal.write_bytes(raw.replace(b"src/alpha.py", b"src/omega.py"))

    with pytest.raises(ReadPathDerivationError) as excinfo:
        derive_task_read_set("t1", journal, tmp_path)
    assert excinfo.value.reason == ReadPathDerivationError.REASON_BROKEN_CHAIN


def test_a_missing_journal_refuses_rather_than_recording_an_empty_read_set(tmp_path: Path) -> None:
    """ "No journal" must not project as "read nothing"."""
    with pytest.raises(ReadPathDerivationError) as excinfo:
        derive_task_read_set("t1", tmp_path / "absent.jsonl", tmp_path)
    assert excinfo.value.reason == ReadPathDerivationError.REASON_MISSING


def test_out_of_tree_reads_are_carried_not_dropped(tmp_path: Path) -> None:
    outside = tmp_path.parent / "elsewhere.py"
    journal = _journal(
        tmp_path,
        [{"event": "read", "path": "src/alpha.py"}, {"event": "read", "path": str(outside)}],
    )

    derived = derive_task_read_set("t1", journal, tmp_path)

    assert derived.read_paths == ("src/alpha.py",)
    assert derived.out_of_tree and all(p.endswith("elsewhere.py") for p in derived.out_of_tree)


# ---------------------------------------------------------------------------
# Receipt projection
# ---------------------------------------------------------------------------


def test_receipt_without_read_sets_is_byte_identical_to_the_old_shape() -> None:
    """Back-compat by omission, so no stored receipt is invalidated.

    The field is absent rather than empty when nothing is supplied, which is
    why ``ADMISSION_RECEIPT_VERSION`` does not move. This mirrors the
    journal's own ``effort`` dimension.
    """
    receipt = build_admission_receipt(_DIGEST, [_task("a"), _task("b")])

    assert "read_sets" not in receipt
    assert verify_admission_receipt(receipt, graph_digest_value=_DIGEST).status == RECEIPT_CONSISTENT_ONLY


def test_read_set_projection_is_byte_identical_regardless_of_input_order(tmp_path: Path) -> None:
    """Two operators recording one decision produce one document."""
    journal = _journal(
        tmp_path,
        [{"event": "read", "path": "src/beta.py"}, {"event": "read", "path": "src/alpha.py"}],
    )
    tasks = [_task("a"), _task("b")]
    reads = [
        derive_task_read_set("a", journal, tmp_path),
        derive_task_read_set("b", journal, tmp_path),
    ]

    first = json.dumps(build_admission_receipt(_DIGEST, tasks, reads), sort_keys=True)
    second = json.dumps(
        build_admission_receipt(_DIGEST, list(reversed(tasks)), list(reversed(reads))),
        sort_keys=True,
    )

    assert first == second
    assert json.loads(first)["read_sets"][0]["read_paths"] == ["src/alpha.py", "src/beta.py"]


def test_receipt_carrying_read_sets_verifies(tmp_path: Path) -> None:
    journal = _journal(tmp_path, [{"event": "read", "path": "src/alpha.py"}])
    receipt = build_admission_receipt(_DIGEST, [_task("a")], [derive_task_read_set("a", journal, tmp_path)])

    assert verify_admission_receipt(receipt, graph_digest_value=_DIGEST).status == RECEIPT_CONSISTENT_ONLY


def test_edited_read_set_is_re_derived_from_the_journal_and_rejected(tmp_path: Path) -> None:
    """Widening a recorded read set by hand fails against the journal.

    The same property the recorded verdicts already have: an edited field is
    caught, or the record is decoration. The edit here is *canonical* --
    sorted, well-shaped -- so only the journal can refute it.
    """
    journal = _journal(tmp_path, [{"event": "read", "path": "src/alpha.py"}])
    receipt = build_admission_receipt(_DIGEST, [_task("a")], [derive_task_read_set("a", journal, tmp_path)])

    receipt["read_sets"][0]["read_paths"] = ["src/alpha.py", "src/injected.py"]  # type: ignore[index]

    result = verify_admission_receipt(
        receipt,
        graph_digest_value=_DIGEST,
        read_sets=[derive_task_read_set("a", journal, tmp_path)],
    )
    assert result.status == RECEIPT_DIVERGED
    assert not result.ok


def test_a_canonical_edit_survives_when_the_journal_is_not_supplied(tmp_path: Path) -> None:
    """Names the limit of the weaker check rather than leaving it implied.

    Without the journals the recorded read side is only checked for canonical
    form, exactly as the node sets are only re-folded without the graph
    document. A caller who wants the stronger guarantee has to supply the
    journals, so nobody gets the weaker one believing it is the stronger.
    """
    journal = _journal(tmp_path, [{"event": "read", "path": "src/alpha.py"}])
    receipt = build_admission_receipt(_DIGEST, [_task("a")], [derive_task_read_set("a", journal, tmp_path)])

    receipt["read_sets"][0]["read_paths"] = ["src/alpha.py", "src/injected.py"]  # type: ignore[index]

    assert verify_admission_receipt(receipt, graph_digest_value=_DIGEST).status == RECEIPT_CONSISTENT_ONLY


def test_an_honest_receipt_verifies_against_its_journals(tmp_path: Path) -> None:
    journal = _journal(tmp_path, [{"event": "read", "path": "src/alpha.py"}])
    receipt = build_admission_receipt(_DIGEST, [_task("a")], [derive_task_read_set("a", journal, tmp_path)])

    result = verify_admission_receipt(
        receipt,
        graph_digest_value=_DIGEST,
        read_sets=[derive_task_read_set("a", journal, tmp_path)],
    )
    assert result.status == RECEIPT_CONSISTENT_ONLY


def test_unsorted_recorded_read_paths_are_a_divergence(tmp_path: Path) -> None:
    """Canonical form is enforced, not repaired."""
    journal = _journal(
        tmp_path,
        [{"event": "read", "path": "src/alpha.py"}, {"event": "read", "path": "src/beta.py"}],
    )
    receipt = build_admission_receipt(_DIGEST, [_task("a")], [derive_task_read_set("a", journal, tmp_path)])

    receipt["read_sets"][0]["read_paths"] = ["src/beta.py", "src/alpha.py"]  # type: ignore[index]

    result = verify_admission_receipt(receipt, graph_digest_value=_DIGEST)
    assert result.status == RECEIPT_DIVERGED
    assert not result.ok


def test_an_explicitly_empty_read_sets_list_is_not_canonical() -> None:
    """Omission is how "no read sets" is spelled; ``[]`` is a different document."""
    receipt = build_admission_receipt(_DIGEST, [_task("a")])
    receipt["read_sets"] = []

    assert verify_admission_receipt(receipt, graph_digest_value=_DIGEST).status == RECEIPT_DIVERGED


def test_read_set_for_a_task_the_receipt_does_not_carry_is_refused(tmp_path: Path) -> None:
    journal = _journal(tmp_path, [{"event": "read", "path": "src/alpha.py"}])

    with pytest.raises(ValueError, match="absent from the receipt"):
        build_admission_receipt(_DIGEST, [_task("a")], [derive_task_read_set("ghost", journal, tmp_path)])


def test_two_read_sets_for_one_task_are_refused(tmp_path: Path) -> None:
    journal = _journal(tmp_path, [{"event": "read", "path": "src/alpha.py"}])
    derived = derive_task_read_set("a", journal, tmp_path)

    with pytest.raises(ValueError, match="more than once"):
        build_admission_receipt(_DIGEST, [_task("a")], [derived, derived])


def test_a_recorded_phantom_read_set_is_a_divergence_not_a_crash() -> None:
    """A verifier owes a verdict, never a traceback."""
    receipt = build_admission_receipt(_DIGEST, [_task("a")])
    receipt["read_sets"] = [{"task_id": "ghost", "read_paths": ["src/x.py"], "out_of_tree": []}]

    result = verify_admission_receipt(receipt, graph_digest_value=_DIGEST)
    assert result.status == RECEIPT_DIVERGED
    assert result.divergences


# ---------------------------------------------------------------------------
# Chunk annotation
# ---------------------------------------------------------------------------


def test_chunk_read_set_is_empty_until_a_journal_derived_set_is_attached() -> None:
    """Chunk detection scrapes the agent's prose; it must not fill this in."""
    chunk = IncrementalChunk(chunk_id="c1", task_id="t1", files=("src/beta.py",), quality_gate_passed=True)

    assert chunk.read_set == ()


def test_with_read_set_attaches_the_derived_paths(tmp_path: Path) -> None:
    journal = _journal(
        tmp_path,
        [{"event": "read", "path": "src/beta.py"}, {"event": "read", "path": "src/alpha.py"}],
    )
    chunk = IncrementalChunk(chunk_id="c1", task_id="t1", files=("src/gamma.py",), quality_gate_passed=True)

    annotated = with_read_set(chunk, derive_task_read_set("t1", journal, tmp_path))

    assert annotated.read_set == ("src/alpha.py", "src/beta.py")
    assert annotated.files == ("src/gamma.py",)  # the write side is untouched
    assert chunk.read_set == ()  # the input is frozen and left alone


def test_with_read_set_refuses_another_tasks_read_set(tmp_path: Path) -> None:
    """One task's reads on another's chunk would answer about the wrong task."""
    journal = _journal(tmp_path, [{"event": "read", "path": "src/alpha.py"}])
    chunk = IncrementalChunk(chunk_id="c1", task_id="t1", files=(), quality_gate_passed=True)

    with pytest.raises(ValueError, match="cannot annotate a chunk"):
        with_read_set(chunk, derive_task_read_set("other", journal, tmp_path))


def test_a_hand_built_read_set_is_accepted_but_carries_no_journal_head() -> None:
    """The type is not a provenance barrier, and must not be described as one.

    ``TaskReadSet`` is an ordinary dataclass: a caller can build one saying
    anything, and both ``with_read_set`` and ``build_admission_receipt`` take
    it. What separates it from a derived set is the absent ``journal_head`` --
    evidence, not a constructor guard. Pinned so nobody reintroduces the
    claim that forging one is impossible.
    """
    chunk = IncrementalChunk(chunk_id="c1", task_id="t1", files=(), quality_gate_passed=True)
    forged = TaskReadSet(task_id="t1", read_paths=("src/whatever_i_say.py",), out_of_tree=())

    assert forged.journal_head == ""
    assert with_read_set(chunk, forged).read_set == ("src/whatever_i_say.py",)

    receipt = build_admission_receipt(_DIGEST, [_task("t1")], [forged])
    assert receipt["read_sets"][0]["journal_head"] == ""  # type: ignore[index]


def test_with_read_set_drops_out_of_tree_reads(tmp_path: Path) -> None:
    """Chunk read sets are intersected against diffs, so in-tree paths only."""
    outside = tmp_path.parent / "elsewhere.py"
    journal = _journal(
        tmp_path,
        [{"event": "read", "path": "src/alpha.py"}, {"event": "read", "path": str(outside)}],
    )
    chunk = IncrementalChunk(chunk_id="c1", task_id="t1", files=(), quality_gate_passed=True)
    derived = derive_task_read_set("t1", journal, tmp_path)

    annotated = with_read_set(chunk, derived)

    assert annotated.read_set == ("src/alpha.py",)
    # Dropped from the chunk, still available on the derived set itself.
    assert derived.out_of_tree


# ---------------------------------------------------------------------------
# Journal binding, and the status a receipt may reach without it
# ---------------------------------------------------------------------------


def _graph(root: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[SemanticCodeGraph, bytes, str]:
    """A one-file semantic graph plus its document and digest."""
    (root / "src" / "pkg").mkdir(parents=True, exist_ok=True)
    (root / "src" / "pkg" / "alpha.py").write_text(_ALPHA_SRC, encoding="utf-8")
    monkeypatch.setattr(semantic_graph, "_git_ls_files", lambda _w: ["src/pkg/alpha.py"])
    built = build_semantic_graph(root)
    return SemanticCodeGraph(built), graph_document(built), graph_digest(built)


def test_a_derived_read_set_records_the_journal_head_it_came_from(tmp_path: Path) -> None:
    """The set names one journal state, so a verifier knows what to re-derive.

    Without this the receipt says which paths were read but not according to
    which journal, and "receipt-anchored" would be a claim with nothing
    behind it.
    """
    journal = _journal(tmp_path, [{"event": "read", "path": "src/alpha.py"}])

    derived = derive_task_read_set("t1", journal, tmp_path)

    assert derived.journal_head
    receipt = build_admission_receipt(_DIGEST, [_task("t1")], [derived])
    assert receipt["read_sets"][0]["journal_head"] == derived.journal_head  # type: ignore[index]


def test_the_journal_head_moves_when_the_journal_does(tmp_path: Path) -> None:
    """Two different journals cannot mint the same head for the same paths."""
    one = _journal(tmp_path, [{"event": "read", "path": "src/alpha.py"}], run_id="run-one")
    two = _journal(
        tmp_path,
        [{"event": "read", "path": "src/alpha.py"}, {"event": "step", "value": 1}],
        run_id="run-two",
    )

    first = derive_task_read_set("t1", one, tmp_path)
    second = derive_task_read_set("t1", two, tmp_path)

    assert first.read_paths == second.read_paths
    assert first.journal_head != second.journal_head


def test_an_unverified_read_side_cannot_reach_verified(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A graph document alone must not certify a read side nobody re-derived.

    The asymmetry this pins: absent node-set evidence already caps the status
    at ``consistent_only``, and absent read-side evidence has to cap it the
    same way. Otherwise an edited-but-canonical read set rides to
    ``verified`` / ``ok`` on the graph document's coat-tails.
    """
    graph, document, digest = _graph(tmp_path, monkeypatch)
    journal = _journal(tmp_path, [{"event": "read", "path": "src/pkg/alpha.py"}])
    task = attribute_task(graph, "a", ["src/pkg/alpha.py"])
    reads = [derive_task_read_set("a", journal, tmp_path)]

    receipt = build_admission_receipt(digest, [task], reads)
    receipt["read_sets"][0]["read_paths"] = ["src/pkg/alpha.py", "src/pkg/injected.py"]  # type: ignore[index]

    without = verify_admission_receipt(receipt, graph_digest_value=digest, graph_document_bytes=document)
    assert without.status == RECEIPT_CONSISTENT_ONLY
    assert not without.ok
    assert not without.read_sets_verified

    with_journals = verify_admission_receipt(
        receipt, graph_digest_value=digest, graph_document_bytes=document, read_sets=reads
    )
    assert with_journals.status == RECEIPT_DIVERGED
    assert not with_journals.ok


def test_an_honest_receipt_reaches_verified_when_both_sides_are_re_derived(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The cap must not make ``verified`` unreachable for a sound receipt."""
    graph, document, digest = _graph(tmp_path, monkeypatch)
    journal = _journal(tmp_path, [{"event": "read", "path": "src/pkg/alpha.py"}])
    task = attribute_task(graph, "a", ["src/pkg/alpha.py"])
    reads = [derive_task_read_set("a", journal, tmp_path)]
    receipt = build_admission_receipt(digest, [task], reads)

    result = verify_admission_receipt(
        receipt, graph_digest_value=digest, graph_document_bytes=document, read_sets=reads
    )

    assert result.status == RECEIPT_VERIFIED
    assert result.ok
    assert result.read_sets_verified


def test_a_receipt_with_no_read_sets_still_reaches_verified(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The cap is vacuous where there is no read side, so nothing regresses."""
    graph, document, digest = _graph(tmp_path, monkeypatch)
    task = attribute_task(graph, "a", ["src/pkg/alpha.py"])
    receipt = build_admission_receipt(digest, [task])

    result = verify_admission_receipt(receipt, graph_digest_value=digest, graph_document_bytes=document)

    assert result.status == RECEIPT_VERIFIED
    assert result.ok
    assert result.read_sets_verified


def test_a_forged_read_set_fails_against_the_real_journal(tmp_path: Path) -> None:
    """Evidence, not the type, is what rejects an invented read set."""
    journal = _journal(tmp_path, [{"event": "read", "path": "src/alpha.py"}])
    forged = TaskReadSet(task_id="a", read_paths=("src/whatever_i_say.py",), out_of_tree=())

    receipt = build_admission_receipt(_DIGEST, [_task("a")], [forged])

    result = verify_admission_receipt(
        receipt,
        graph_digest_value=_DIGEST,
        read_sets=[derive_task_read_set("a", journal, tmp_path)],
    )
    assert result.status == RECEIPT_DIVERGED
    assert not result.ok
