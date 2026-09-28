"""Tests for the PROV-O export projection (issue #5039).

Each test is named for the property it protects, per the issue's
acceptance list. Test 5 (ontology validation) and test 7 (export
signature) belong to later slices of #5039 and are not covered here;
see the PR body.
"""

from __future__ import annotations

import re
from unittest import mock

import pytest

from bernstein.core.lineage.entry import LineageEntry, ModelRef, entry_hash
from bernstein.core.lineage.prov_export import (
    ProvExportError,
    canonical_prov_json_bytes,
    project_prov_ancestry,
    to_prov_json,
    to_turtle,
)


def _h(seed: str) -> str:
    return "sha256:" + (seed * 64)[:64]


def _mk_entry(
    *,
    artefact_path: str = "x.py",
    content_hash: str,
    parent_hashes: list[str] | None = None,
    agent_id: str = "agent:a",
    agent_card_kid: str = "k1",
    ts_ns: int = 1_700_000_000_000_000_000,
    attachment_digests: list[str] | None = None,
    model_ref: ModelRef | None = None,
    trust_class: str | None = None,
) -> LineageEntry:
    return LineageEntry(
        v=1,
        artefact_path=artefact_path,
        artefact_kind="file",
        content_hash=content_hash,
        parent_hashes=parent_hashes or [],
        agent_id=agent_id,
        agent_card_kid=agent_card_kid,
        tool_call_id="tc-1",
        span_id="span-1",
        ts_ns=ts_ns,
        operator_hmac="deadbeef" * 8,
        attachment_digests=attachment_digests,
        model_ref=model_ref,
        trust_class=trust_class,
    )


# ── 1. determinism ───────────────────────────────────────────────────────────


def test_prov_export_is_byte_identical_across_repeated_runs() -> None:
    root = _mk_entry(content_hash=_h("1"))
    child = _mk_entry(content_hash=_h("2"), parent_hashes=[entry_hash(root)])
    entries = [root, child]

    with mock.patch("time.time", return_value=1_000_000):
        first = canonical_prov_json_bytes(project_prov_ancestry(entries, root_entry_hash=entry_hash(child)))
    with mock.patch("time.time", return_value=2_000_000):
        # Reversed input order must not change the output: the projection
        # sorts entities/activities/agents/relations itself.
        second = canonical_prov_json_bytes(
            project_prov_ancestry(list(reversed(entries)), root_entry_hash=entry_hash(child))
        )

    assert first == second


# ── 2. entity URI embeds the content hash ────────────────────────────────────


def test_entity_uri_embeds_the_content_hash() -> None:
    root = _mk_entry(content_hash=_h("1"))
    doc = project_prov_ancestry([root], root_entry_hash=entry_hash(root))

    assert doc.root_entity_id.endswith(_h("1"))
    assert _h("1") in doc.root_entity_id

    payload = to_prov_json(doc)
    assert doc.root_entity_id in payload["entity"]


# ── 3. parent_hashes -> wasDerivedFrom ───────────────────────────────────────


def test_parent_hashes_become_was_derived_from_edges() -> None:
    root = _mk_entry(content_hash=_h("1"))
    child = _mk_entry(content_hash=_h("2"), parent_hashes=[entry_hash(root)])
    doc = project_prov_ancestry([root, child], root_entry_hash=entry_hash(child))

    derived = [r for r in doc.relations if r.kind == "wasDerivedFrom"]
    assert len(derived) == 1
    assert derived[0].subject == doc.root_entity_id
    assert derived[0].obj.endswith(_h("1"))

    payload = to_prov_json(doc)
    assert "wasDerivedFrom" in payload
    (rel,) = payload["wasDerivedFrom"].values()
    assert rel["prov:generatedEntity"] == doc.root_entity_id
    assert rel["prov:usedEntity"].endswith(_h("1"))


def test_ancestry_omits_entries_outside_the_closure() -> None:
    root = _mk_entry(content_hash=_h("1"))
    child = _mk_entry(content_hash=_h("2"), parent_hashes=[entry_hash(root)])
    unrelated = _mk_entry(artefact_path="other.py", content_hash=_h("9"))
    doc = project_prov_ancestry([root, child, unrelated], root_entry_hash=entry_hash(child))

    ids = {e.id for e in doc.entities}
    assert not any(_h("9") in i for i in ids)
    assert len(doc.entities) == 2


# ── 4. attachment_digests -> used ────────────────────────────────────────────


def test_attachment_digests_become_used_edges() -> None:
    digest = "ab" * 32
    root = _mk_entry(content_hash=_h("1"), attachment_digests=[digest])
    doc = project_prov_ancestry([root], root_entry_hash=entry_hash(root))

    used = [r for r in doc.relations if r.kind == "used"]
    assert len(used) == 1
    assert used[0].obj.endswith(f"sha256:{digest}")

    payload = to_prov_json(doc)
    assert "used" in payload
    (rel,) = payload["used"].values()
    assert rel["prov:entity"].endswith(f"sha256:{digest}")


# ── 8. no export-time timestamps or generated identifiers ───────────────────


def test_export_contains_no_export_time_timestamps_or_generated_identifiers() -> None:
    root = _mk_entry(content_hash=_h("1"))
    child = _mk_entry(content_hash=_h("2"), parent_hashes=[entry_hash(root)])
    entries = [root, child]

    with mock.patch("time.time", return_value=1_000_000):
        first = canonical_prov_json_bytes(project_prov_ancestry(entries, root_entry_hash=entry_hash(child)))
    with mock.patch("time.time", return_value=99_999_999):
        second = canonical_prov_json_bytes(project_prov_ancestry(entries, root_entry_hash=entry_hash(child)))

    # A wall-clock export timestamp would make these two diverge.
    assert first == second

    text = first.decode("utf-8")
    assert "exported_at" not in text
    assert "export_time" not in text
    # No random/generated UUID (8-4-4-4-12 hex) anywhere in the document --
    # every id is a stable index or a value copied from the entries.
    uuid_pattern = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
    assert not uuid_pattern.search(text)


# ── unproducible without the entry ───────────────────────────────────────────


def test_missing_root_entry_raises_prov_export_error() -> None:
    root = _mk_entry(content_hash=_h("1"))
    with pytest.raises(ProvExportError):
        project_prov_ancestry([root], root_entry_hash=_h("not-present"))


# ── slice 2: Turtle, from the same intermediate ──────────────────────────────


def test_turtle_embeds_content_hash_and_derivation() -> None:
    root = _mk_entry(content_hash=_h("1"))
    child = _mk_entry(content_hash=_h("2"), parent_hashes=[entry_hash(root)])
    doc = project_prov_ancestry([root, child], root_entry_hash=entry_hash(child))

    turtle = to_turtle(doc)
    assert _h("1") in turtle
    assert _h("2") in turtle
    assert "prov:wasDerivedFrom" in turtle
    assert "@prefix prov:" in turtle


def test_turtle_is_byte_identical_across_repeated_runs() -> None:
    root = _mk_entry(content_hash=_h("1"))
    child = _mk_entry(content_hash=_h("2"), parent_hashes=[entry_hash(root)])
    doc_a = project_prov_ancestry([root, child], root_entry_hash=entry_hash(child))
    doc_b = project_prov_ancestry(list(reversed([root, child])), root_entry_hash=entry_hash(child))

    assert to_turtle(doc_a) == to_turtle(doc_b)


# ── 6. round trip ────────────────────────────────────────────────────────────


def _parse_prov_json(payload: dict[str, object]) -> tuple[set[str], set[tuple[str, str, str]]]:
    """Recover the PROV node and edge set from a PROV-JSON document alone.

    Nodes are every key under ``entity``/``activity``/``agent``; edges are
    every relation in the four PROV relation maps, normalised to
    ``(kind, subject, object)``. This is the "back" half of the round trip and
    shares no code with :func:`project_prov_ancestry`, so it cannot be fooled
    by a lossy mapping that emits the same omission on both sides.
    """
    nodes = set(payload["entity"]) | set(payload["activity"]) | set(payload["agent"])
    edges: set[tuple[str, str, str]] = set()
    relation_roles = {
        "wasGeneratedBy": ("prov:entity", "prov:activity"),
        "used": ("prov:activity", "prov:entity"),
        "wasAssociatedWith": ("prov:activity", "prov:agent"),
        "wasDerivedFrom": ("prov:generatedEntity", "prov:usedEntity"),
    }
    for kind, (subject_key, object_key) in relation_roles.items():
        for relation in payload.get(kind, {}).values():
            edges.add((kind, relation[subject_key], relation[object_key]))
    return nodes, edges


def _expected_nodes_and_edges(
    entries: list[LineageEntry], root_entry_hash: str
) -> tuple[set[str], set[tuple[str, str, str]]]:
    """Derive the source node and edge set directly from the entries.

    This is the mapping from issue #5039 written out independently of the
    projection: artefacts and attachments become content-hash-derived entity
    URIs, turns become entry-hash-derived activities, and each lineage agent
    identity ``(agent_id, agent_card_kid)`` becomes its own agent node.
    """
    by_hash = {entry_hash(e): e for e in entries}
    closure: set[str] = set()
    frontier = [root_entry_hash]
    while frontier:
        h = frontier.pop()
        if h in closure:
            continue
        closure.add(h)
        entry = by_hash.get(h)
        if entry is not None:
            frontier.extend(entry.parent_hashes)

    nodes: set[str] = set()
    edges: set[tuple[str, str, str]] = set()
    for h in sorted(closure):
        entry = by_hash[h]
        entity_id = f"urn:bernstein:entity:{entry.content_hash}"
        activity_id = f"urn:bernstein:activity:{h}"
        operator_id = f"urn:bernstein:agent:{entry.agent_id}:{entry.agent_card_kid}"
        nodes.add(entity_id)
        nodes.add(activity_id)
        nodes.add(operator_id)
        edges.add(("wasGeneratedBy", entity_id, activity_id))
        edges.add(("wasAssociatedWith", activity_id, operator_id))
        if entry.model_ref is not None:
            model_id = f"urn:bernstein:agent:model:{entry.model_ref.provider}:{entry.model_ref.model_requested}"
            nodes.add(model_id)
            edges.add(("wasAssociatedWith", activity_id, model_id))
        for parent_hash in entry.parent_hashes:
            parent = by_hash.get(parent_hash)
            if parent is None:
                continue
            edges.add(("wasDerivedFrom", entity_id, f"urn:bernstein:entity:{parent.content_hash}"))
        for digest in entry.attachment_digests or ():
            attachment_id = f"urn:bernstein:entity:sha256:{digest}"
            nodes.add(attachment_id)
            edges.add(("used", activity_id, attachment_id))
    return nodes, edges


def test_round_trip_preserves_the_node_and_edge_set() -> None:
    root = _mk_entry(content_hash=_h("1"), agent_id="agent:a", agent_card_kid="k1")
    child = _mk_entry(
        content_hash=_h("2"),
        parent_hashes=[entry_hash(root)],
        agent_id="agent:a",
        agent_card_kid="k2",
        attachment_digests=["ab" * 32],
        model_ref=ModelRef(provider="openai", model_requested="gpt-4"),
    )
    entries = [root, child]

    doc = project_prov_ancestry(entries, root_entry_hash=entry_hash(child))
    payload = to_prov_json(doc)
    recovered_nodes, recovered_edges = _parse_prov_json(payload)
    expected_nodes, expected_edges = _expected_nodes_and_edges(entries, entry_hash(child))

    assert recovered_nodes == expected_nodes
    assert recovered_edges == expected_edges
