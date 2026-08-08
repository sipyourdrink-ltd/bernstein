"""Task attribution and parallel-admission verdicts (#3237, steps 2-3).

The property under test throughout is that a verdict is a derivation: given the
same graph and the same tasks, the same answer comes out, and an answer is only
``ADMIT_PARALLEL`` when the evidence supports it. The failure these tests exist
to catch is a confident "disjoint" resting on a coverage hole.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import bernstein.core.knowledge.ast_symbol_graph as semantic_graph
from bernstein.core.knowledge.ast_symbol_graph import build_semantic_graph, graph_from_document
from bernstein.core.knowledge.code_graph import (
    ATTRIBUTION_PROVEN,
    ATTRIBUTION_UNPROVEN,
    REASON_INDEX_TRUNCATED,
    REASON_INFERRED_EDGE,
    REASON_PATH_NOT_INDEXED,
    CodeGraph,
    SemanticCodeGraph,
    TaskNodeSet,
    attribute_task,
)
from bernstein.core.parallel_admission import (
    ADMIT_PARALLEL,
    RECEIPT_CONSISTENT_ONLY,
    RECEIPT_DIVERGED,
    RECEIPT_GRAPH_MISMATCH,
    RECEIPT_VERIFIED,
    SERIALISE_OVERLAP,
    SERIALISE_UNPROVEN,
    admit_pair,
    build_admission_receipt,
    serial_groups,
    verify_admission_receipt,
)


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _graph_for(root: Path, files: dict[str, str], monkeypatch: pytest.MonkeyPatch) -> CodeGraph:
    for rel, body in files.items():
        _write(root / rel, body)
    listing = sorted(files)
    monkeypatch.setattr(semantic_graph, "_git_ls_files", lambda _w: listing)
    return SemanticCodeGraph(build_semantic_graph(root))


#: Two independent islands: alpha calls its own helper, beta calls its own.
#: Nothing connects them, so a correct implementation admits them in parallel.
_DISJOINT = {
    "src/pkg/alpha.py": "def alpha_helper() -> int:\n    return 1\n",
    "src/pkg/alpha_main.py": (
        "from pkg.alpha import alpha_helper\n\ndef alpha_run() -> int:\n    return alpha_helper()\n"
    ),
    "src/pkg/beta.py": "def beta_helper() -> int:\n    return 2\n",
    "src/pkg/beta_main.py": ("from pkg.beta import beta_helper\n\ndef beta_run() -> int:\n    return beta_helper()\n"),
}


# ---------------------------------------------------------------------------
# Attribution
# ---------------------------------------------------------------------------


def test_attribution_is_deterministic_across_graph_builds(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Same tree, different enumeration order, identical attribution."""
    for rel, body in _DISJOINT.items():
        _write(tmp_path / rel, body)
    listing = sorted(_DISJOINT)

    monkeypatch.setattr(semantic_graph, "_git_ls_files", lambda _w: listing)
    first = attribute_task(SemanticCodeGraph(build_semantic_graph(tmp_path)), "t1", ["src/pkg/alpha_main.py"])

    monkeypatch.setattr(semantic_graph, "_git_ls_files", lambda _w: list(reversed(listing)))
    second = attribute_task(SemanticCodeGraph(build_semantic_graph(tmp_path)), "t1", ["src/pkg/alpha_main.py"])

    assert first == second
    assert first.neighborhood == tuple(sorted(first.neighborhood))


def test_declared_path_absent_from_the_index_is_unproven(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A path the graph never saw makes the attribution unprovable.

    Silence is not evidence of absence: the task may touch code the indexer
    skipped, so an empty seed set must not read as an empty footprint.
    """
    graph = _graph_for(tmp_path, _DISJOINT, monkeypatch)
    result = attribute_task(graph, "t1", ["src/pkg/never_indexed.py"])

    assert result.verdict == ATTRIBUTION_UNPROVEN
    assert REASON_PATH_NOT_INDEXED in result.reasons


def test_task_declaring_no_paths_is_unproven(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A task that owns nothing is unknown, not harmless."""
    graph = _graph_for(tmp_path, _DISJOINT, monkeypatch)
    result = attribute_task(graph, "t1", [])

    assert result.verdict == ATTRIBUTION_UNPROVEN
    assert REASON_PATH_NOT_INDEXED in result.reasons


def test_inferred_edge_on_the_boundary_is_unproven(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A neighbourhood reached over a guessed edge cannot be proven.

    This is the test that stops the whole feature from becoming a heuristic
    with extra steps: the graph resolves an ambiguous name by taking the first
    candidate, so an edge produced that way may attribute a symbol to the wrong
    task.
    """
    ambiguous = {
        "src/pkg/alpha.py": "def shared() -> int:\n    return 1\n",
        "src/pkg/beta.py": "def shared() -> int:\n    return 2\n",
        "src/pkg/caller.py": "def run() -> int:\n    return shared()\n",
    }
    graph = _graph_for(tmp_path, ambiguous, monkeypatch)
    result = attribute_task(graph, "t1", ["src/pkg/caller.py"])

    assert result.verdict == ATTRIBUTION_UNPROVEN
    assert REASON_INFERRED_EDGE in result.reasons


def test_truncated_index_makes_every_attribution_unproven(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A graph that dropped files cannot prove anything about coverage."""
    for rel, body in _DISJOINT.items():
        _write(tmp_path / rel, body)
    listing = sorted(_DISJOINT)
    monkeypatch.setattr(semantic_graph, "_git_ls_files", lambda _w: listing)
    monkeypatch.setattr(semantic_graph, "_MAX_FILES", 2)

    graph = SemanticCodeGraph(build_semantic_graph(tmp_path))
    result = attribute_task(graph, "t1", ["src/pkg/alpha_main.py"])

    assert result.verdict == ATTRIBUTION_UNPROVEN
    assert REASON_INDEX_TRUNCATED in result.reasons


def test_depth_zero_keeps_the_seeds_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    graph = _graph_for(tmp_path, _DISJOINT, monkeypatch)
    result = attribute_task(graph, "t1", ["src/pkg/alpha_main.py"], depth=0)

    assert result.neighborhood == result.seed_symbols


def test_negative_depth_is_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    graph = _graph_for(tmp_path, _DISJOINT, monkeypatch)
    with pytest.raises(ValueError, match="depth must be >= 0"):
        attribute_task(graph, "t1", ["src/pkg/alpha_main.py"], depth=-1)


def test_semantic_code_graph_satisfies_the_protocol(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    graph = _graph_for(tmp_path, _DISJOINT, monkeypatch)
    assert isinstance(graph, CodeGraph)
    assert graph.digest().startswith("sha256:")
    assert graph.document()


# ---------------------------------------------------------------------------
# Admission
# ---------------------------------------------------------------------------


def _proven(task_id: str, symbols: tuple[str, ...]) -> TaskNodeSet:
    return TaskNodeSet(
        task_id=task_id,
        declared_paths=(),
        seed_symbols=symbols,
        neighborhood=symbols,
        depth=1,
        verdict=ATTRIBUTION_PROVEN,
        reasons=(),
    )


def _unproven(task_id: str, symbols: tuple[str, ...], reason: str) -> TaskNodeSet:
    return TaskNodeSet(
        task_id=task_id,
        declared_paths=(),
        seed_symbols=symbols,
        neighborhood=symbols,
        depth=1,
        verdict=ATTRIBUTION_UNPROVEN,
        reasons=(reason,),
    )


def test_disjoint_provable_neighborhoods_admit_parallel(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The end-to-end happy path, over a real graph rather than fixtures."""
    graph = _graph_for(tmp_path, _DISJOINT, monkeypatch)
    left = attribute_task(graph, "alpha", ["src/pkg/alpha_main.py", "src/pkg/alpha.py"])
    right = attribute_task(graph, "beta", ["src/pkg/beta_main.py", "src/pkg/beta.py"])

    assert left.proven and right.proven
    verdict = admit_pair(left, right)
    assert verdict.verdict == ADMIT_PARALLEL
    assert verdict.shared_symbols == ()


def test_shared_symbol_serialises_and_names_the_overlap() -> None:
    verdict = admit_pair(
        _proven("a", ("f.py::one", "f.py::two")),
        _proven("b", ("f.py::two", "f.py::three")),
    )
    assert verdict.verdict == SERIALISE_OVERLAP
    assert verdict.shared_symbols == ("f.py::two",)


def test_unproven_side_serialises_even_when_nothing_intersects() -> None:
    """Provability is checked before intersection, and this pins that order.

    An empty intersection against an unknown boundary means nothing. Admitting
    it would be the exact failure the design exists to prevent.
    """
    verdict = admit_pair(
        _proven("a", ("f.py::one",)),
        _unproven("b", ("g.py::two",), REASON_INFERRED_EDGE),
    )
    assert verdict.verdict == SERIALISE_UNPROVEN
    assert verdict.shared_symbols == ()
    assert REASON_INFERRED_EDGE in verdict.reasons


def test_verdict_does_not_depend_on_argument_order() -> None:
    left = _proven("b", ("f.py::two",))
    right = _proven("a", ("f.py::two",))
    assert admit_pair(left, right) == admit_pair(right, left)


def test_serial_groups_separates_independent_tasks() -> None:
    groups = serial_groups(
        [
            _proven("a", ("f.py::one",)),
            _proven("b", ("g.py::two",)),
            _proven("c", ("f.py::one",)),
        ]
    )
    # a and c share a symbol; b is independent.
    assert groups == (("a", "c"), ("b",))


def test_serial_groups_is_independent_of_input_order() -> None:
    tasks = [
        _proven("a", ("f.py::one",)),
        _proven("b", ("g.py::two",)),
        _proven("c", ("f.py::one",)),
    ]
    assert serial_groups(tasks) == serial_groups(list(reversed(tasks)))


def test_one_unproven_task_pulls_its_peers_into_its_group() -> None:
    """Unknown coverage costs parallelism, never correctness."""
    groups = serial_groups(
        [
            _proven("a", ("f.py::one",)),
            _proven("b", ("g.py::two",)),
            _unproven("c", ("h.py::three",), REASON_PATH_NOT_INDEXED),
        ]
    )
    assert groups == (("a", "b", "c"),)


# ---------------------------------------------------------------------------
# Receipt projection (#3237, step 4)
# ---------------------------------------------------------------------------


def test_receipt_round_trips_and_verifies() -> None:
    tasks = [_proven("a", ("f.py::one",)), _proven("b", ("g.py::two",))]
    receipt = build_admission_receipt("sha256:abc", tasks)

    assert receipt["graph_digest"] == "sha256:abc"
    # No document supplied, so this is the weaker check by construction.
    assert verify_admission_receipt(receipt, graph_digest_value="sha256:abc").status == (RECEIPT_CONSISTENT_ONLY)


def test_receipt_is_byte_identical_for_the_same_inputs() -> None:
    """Two operators building a receipt over one decision produce one document."""
    tasks = [_proven("b", ("g.py::two",)), _proven("a", ("f.py::one",))]
    first = json.dumps(build_admission_receipt("sha256:abc", tasks), sort_keys=True)
    second = json.dumps(build_admission_receipt("sha256:abc", list(reversed(tasks))), sort_keys=True)
    assert first == second


def test_edited_verdict_is_re_derived_and_rejected() -> None:
    """The recorded verdict is never trusted.

    This is the property that makes the receipt worth anything: flipping a
    serialisation into an admission by editing the file has to fail, or the
    receipt is decoration.
    """
    tasks = [_proven("a", ("f.py::one",)), _proven("b", ("f.py::one",))]
    receipt = build_admission_receipt("sha256:abc", tasks)
    assert receipt["pairs"][0]["verdict"] == SERIALISE_OVERLAP  # type: ignore[index]

    receipt["pairs"][0]["verdict"] = ADMIT_PARALLEL  # type: ignore[index]
    receipt["pairs"][0]["shared_symbols"] = []  # type: ignore[index]

    result = verify_admission_receipt(receipt, graph_digest_value="sha256:abc")
    assert not result.ok
    assert result.status == RECEIPT_DIVERGED
    assert result.divergences


def test_receipt_for_another_graph_is_reported_as_a_mismatch() -> None:
    """A wrong-graph receipt is a different problem from a tampered one."""
    receipt = build_admission_receipt("sha256:abc", [_proven("a", ("f.py::one",))])
    result = verify_admission_receipt(receipt, graph_digest_value="sha256:def")

    assert result.status == RECEIPT_GRAPH_MISMATCH
    assert not result.ok


def test_receipt_carries_the_serial_groups_it_implies() -> None:
    tasks = [
        _proven("a", ("f.py::one",)),
        _proven("b", ("f.py::one",)),
        _proven("c", ("g.py::two",)),
    ]
    receipt = build_admission_receipt("sha256:abc", tasks)
    assert receipt["serial_groups"] == [["a", "b"], ["c"]]


# ---------------------------------------------------------------------------
# Offline re-derivation (#3237, step 4 completed)
# ---------------------------------------------------------------------------


def test_graph_document_round_trips(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A document rebuilds into a graph that serialises back to the same bytes.

    This is what a verifier depends on: a document that rebuilt into something
    serialising differently would let a tampered graph pass as the original.
    """
    graph = _graph_for(tmp_path, _DISJOINT, monkeypatch)
    document = graph.document()
    rebuilt = SemanticCodeGraph(graph_from_document(document))

    assert rebuilt.document() == document
    assert rebuilt.digest() == graph.digest()


def test_full_verification_re_derives_the_node_sets(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """With the document, attribution itself is recomputed, not read."""
    graph = _graph_for(tmp_path, _DISJOINT, monkeypatch)
    tasks = [
        attribute_task(graph, "alpha", ["src/pkg/alpha_main.py", "src/pkg/alpha.py"]),
        attribute_task(graph, "beta", ["src/pkg/beta_main.py", "src/pkg/beta.py"]),
    ]
    receipt = build_admission_receipt(graph.digest(), tasks)

    result = verify_admission_receipt(
        receipt,
        graph_digest_value=graph.digest(),
        graph_document_bytes=graph.document(),
    )
    assert result.ok
    assert result.status == RECEIPT_VERIFIED


def test_edited_node_set_is_caught_only_with_the_document(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The two verification strengths differ exactly where it matters.

    Widening a node set so two tasks stop intersecting is invisible to a
    consistency check -- the recorded verdicts still follow from the recorded
    sets. Only re-deriving the sets from the graph catches it.
    """
    graph = _graph_for(tmp_path, _DISJOINT, monkeypatch)
    tasks = [
        attribute_task(graph, "alpha", ["src/pkg/alpha_main.py", "src/pkg/alpha.py"]),
        attribute_task(graph, "beta", ["src/pkg/beta_main.py", "src/pkg/beta.py"]),
    ]
    receipt = build_admission_receipt(graph.digest(), tasks)
    receipt["tasks"][0]["neighborhood"] = ["src/pkg/fabricated.py::ghost"]  # type: ignore[index]

    without = verify_admission_receipt(receipt, graph_digest_value=graph.digest())
    assert without.status == RECEIPT_CONSISTENT_ONLY
    assert not without.ok

    with_document = verify_admission_receipt(
        receipt,
        graph_digest_value=graph.digest(),
        graph_document_bytes=graph.document(),
    )
    assert with_document.status == RECEIPT_DIVERGED
    assert any("alpha" in d for d in with_document.divergences)


def test_document_not_matching_its_digest_is_a_mismatch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A document handed in under the wrong digest is rejected before use."""
    graph = _graph_for(tmp_path, _DISJOINT, monkeypatch)
    receipt = build_admission_receipt(graph.digest(), [attribute_task(graph, "a", ["src/pkg/alpha.py"])])

    # ``truncated`` is derived from the two counts at serialisation time rather
    # than stored, so editing it does not survive the round trip and is not a
    # tamper the digest can see. Edit an edge's origin instead: that is read
    # back verbatim and is exactly the field a disjointness verdict rests on.
    # ``origin`` is read back verbatim and is exactly the field a disjointness
    # verdict rests on. This fixture is all EXTRACTED (which is why it is
    # provable), so the edit goes the other way; the dangerous direction --
    # promoting a guess to EXTRACTED -- is caught by the same digest.
    tampered = graph.document().replace(b'"origin":"EXTRACTED"', b'"origin":"INFERRED"', 1)
    assert tampered != graph.document(), "fixture must contain an edge to tamper with"
    result = verify_admission_receipt(
        receipt,
        graph_digest_value=graph.digest(),
        graph_document_bytes=tampered,
    )
    assert result.status == RECEIPT_GRAPH_MISMATCH


def test_consistency_only_is_not_ok() -> None:
    """The weaker guarantee cannot be obtained by writing ``if result.ok``."""
    receipt = build_admission_receipt("sha256:abc", [_proven("a", ("f.py::one",))])
    result = verify_admission_receipt(receipt, graph_digest_value="sha256:abc")
    assert result.status == RECEIPT_CONSISTENT_ONLY
    assert not result.ok


def test_malformed_document_is_rejected() -> None:
    with pytest.raises(ValueError, match="not valid JSON"):
        graph_from_document(b"{not json")


def test_unknown_document_version_is_rejected() -> None:
    with pytest.raises(ValueError, match="unsupported graph document version"):
        graph_from_document(b'{"version":999,"nodes":[],"edges":[]}')


# ---------------------------------------------------------------------------
# One task id, one attribution
# ---------------------------------------------------------------------------


def test_serial_groups_rejects_two_attributions_for_one_task() -> None:
    """The partition is keyed by task id, so two under one name is not a set.

    Without this the union-find collapses them into a single node and emits a
    group listing the id twice: a scheduling instruction naming a task that
    has two different boundaries, which nobody downstream can act on.
    """
    tasks = [_proven("a", ("f.py::one",)), _proven("a", ("g.py::two",))]

    with pytest.raises(ValueError, match="same task more than once"):
        serial_groups(tasks)


def test_receipt_rejects_two_attributions_for_one_task() -> None:
    """The receipt is refused where it is built, not after it is projected."""
    tasks = [_proven("a", ("f.py::one",)), _proven("a", ("g.py::two",))]

    with pytest.raises(ValueError, match="same task more than once"):
        build_admission_receipt("sha256:abc", tasks)


def test_duplicate_ids_in_a_recorded_receipt_are_a_divergence_not_a_crash() -> None:
    """A verifier owes the caller a verdict, including on a receipt no run produced."""
    receipt = build_admission_receipt("sha256:abc", [_proven("a", ("f.py::one",))])
    receipt["tasks"].append(dict(receipt["tasks"][0]))  # type: ignore[attr-defined,index,arg-type]

    result = verify_admission_receipt(receipt, graph_digest_value="sha256:abc")

    assert result.status == RECEIPT_DIVERGED
    assert not result.ok


# ---------------------------------------------------------------------------
# Verification covers the whole projection, not only the verdicts
# ---------------------------------------------------------------------------


def test_edited_receipt_version_is_a_divergence() -> None:
    """The version travels inside the bytes, so it has to be re-derived too.

    Leaving it out lets a receipt claim a shape it was not written in while
    every verdict still reproduces, which is exactly the comparison a future
    reader would rely on to know the two documents are the same kind of thing.
    """
    receipt = build_admission_receipt("sha256:abc", [_proven("a", ("f.py::one",))])
    receipt["version"] = 999

    result = verify_admission_receipt(receipt, graph_digest_value="sha256:abc")

    assert result.status == RECEIPT_DIVERGED
    assert any("version" in d for d in result.divergences)


def test_padded_task_entry_is_a_divergence() -> None:
    """A ``tasks`` list that is not the canonical projection does not verify.

    The verdicts follow from four of the recorded fields, so everything else in
    an entry could be edited freely while ``pairs`` and ``serial_groups`` still
    reproduced. A receipt is either the document the run produced or it is not.
    """
    receipt = build_admission_receipt("sha256:abc", [_proven("a", ("f.py::one",))])
    receipt["tasks"][0]["owner"] = "someone-else"  # type: ignore[index]

    result = verify_admission_receipt(receipt, graph_digest_value="sha256:abc")

    assert result.status == RECEIPT_DIVERGED
    assert any("tasks" in d for d in result.divergences)


# ---------------------------------------------------------------------------
# A tampered document cannot borrow the untampered digest
# ---------------------------------------------------------------------------


def test_fabricated_edge_in_the_document_does_not_verify(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An edge the loader would drop must not let the document keep its digest.

    ``SemanticGraph.add_edge`` discards an edge whose endpoints it does not
    know. Reconstructing through it means the digest is taken over a graph the
    document does not describe, so a document carrying fabricated edges hashes
    to the original and reads as the graph the decision was taken over.
    """
    graph = _graph_for(tmp_path, _DISJOINT, monkeypatch)
    receipt = build_admission_receipt(graph.digest(), [attribute_task(graph, "a", ["src/pkg/alpha.py"])])

    payload = json.loads(graph.document())
    payload["edges"].append(
        {
            "source": "src/pkg/alpha.py::alpha_helper",
            "target": "src/pkg/ghost.py::ghost",
            "kind": "calls",
            "origin": "EXTRACTED",
        }
    )
    tampered = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")

    result = verify_admission_receipt(
        receipt,
        graph_digest_value=graph.digest(),
        graph_document_bytes=tampered,
    )

    assert result.status == RECEIPT_GRAPH_MISMATCH
    assert not result.ok


def test_unloadable_document_is_a_mismatch_not_an_exception() -> None:
    """A document the loader refuses is reported, not raised through."""
    receipt = build_admission_receipt("sha256:abc", [_proven("a", ("f.py::one",))])

    result = verify_admission_receipt(
        receipt,
        graph_digest_value="sha256:abc",
        graph_document_bytes=b"{not json",
    )

    assert result.status == RECEIPT_GRAPH_MISMATCH
    assert any("could not be loaded" in d for d in result.divergences)
