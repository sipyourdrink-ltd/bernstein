"""PROV-O export: a deterministic projection of the lineage DAG (issue #5039).

Bernstein's lineage v1 store (:mod:`bernstein.core.lineage.entry`,
:mod:`bernstein.core.lineage.store`) is a content-addressed DAG: every
:class:`~bernstein.core.lineage.entry.LineageEntry` names its own
``content_hash`` and the ``parent_hashes`` of the entries it supersedes.
That graph is provenance in everything but vocabulary -- W3C PROV-O is the
vocabulary regulators, research-data systems, and archival tooling already
read, and the tree carries no PROV export at all.

This module makes the PROV-O export a **projection** of the lineage log
rather than a hand-maintained parallel record, following the style of
:mod:`bernstein.core.lineage.c2pa` and
:mod:`bernstein.core.observability.otel_projection`:

* Every PROV entity id embeds the artefact's ``content_hash``, so a
  consumer holding the referenced bytes can verify the node is what it
  claims.
* ``parent_hashes`` become ``prov:wasDerivedFrom`` edges between entities;
  ``attachment_digests`` become ``prov:used`` edges from the producing
  activity onto an entity for each attached input.
* :func:`project_prov_ancestry` is a pure function of the entries it is
  given -- it never reads a clock, environment, or a random source, so two
  exports of the same ancestry produce byte-identical PROV-JSON and Turtle.
  Every relation id is a stable index over a sorted relation list rather
  than a generated UUID, and the only timestamp in the document
  (``prov:generatedAtTime`` / ``prov:endTime``) is derived from the
  entry's own ``ts_ns``, never from the export's wall clock.

Only entries reachable from the requested entry by walking ``parent_hashes``
are exported: the ancestry of one artefact, not the whole log. Signing the
export and validating it against the PROV-O ontology are later slices of
issue #5039 (see the issue's slice list); this module produces the unsigned
document those later slices will sign and validate.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from bernstein.core.lineage.entry import LineageEntry, entry_hash

if TYPE_CHECKING:
    from collections.abc import Sequence

__all__ = [
    "NS_BERNSTEIN",
    "NS_PROV",
    "NS_XSD",
    "PROV_SCHEMA_VERSION",
    "ProvActivity",
    "ProvAgent",
    "ProvDocument",
    "ProvEntity",
    "ProvExportError",
    "ProvRelation",
    "canonical_prov_json_bytes",
    "project_prov_ancestry",
    "to_prov_json",
    "to_turtle",
]

#: Schema version of this projection envelope. Bumped on breaking changes to
#: the exported document shape.
PROV_SCHEMA_VERSION: str = "1.0.0"

NS_PROV: str = "http://www.w3.org/ns/prov#"
NS_XSD: str = "http://www.w3.org/2001/XMLSchema#"
#: Namespace for the bernstein-specific extension attributes (e.g.
#: ``trust_class``) that have no PROV core term -- see the issue's mapping
#: table.
NS_BERNSTEIN: str = "urn:bernstein:"


class ProvExportError(RuntimeError):
    """Raised when a PROV export cannot be built.

    Raised by :func:`project_prov_ancestry` when the requested entry is not
    among the supplied entries: the export is *unproducible* without the
    entry it starts from, not merely empty.
    """


# ---------------------------------------------------------------------------
# Id derivation
# ---------------------------------------------------------------------------


def _entity_id(content_hash: str) -> str:
    """Return the ``prov:Entity`` URI for an artefact version.

    Embeds ``content_hash`` (already ``sha256:``-prefixed) so a verifier
    holding the referenced bytes can confirm the node is what it claims.
    """
    return f"urn:bernstein:entity:{content_hash}"


def _attachment_entity_id(digest_hex: str) -> str:
    """Return the entity URI for an attached input, from its bare hex digest."""
    return _entity_id(f"sha256:{digest_hex}")


def _activity_id(entry_hash_str: str) -> str:
    """Return the ``prov:Activity`` URI for the turn that produced one entry."""
    return f"urn:bernstein:activity:{entry_hash_str}"


def _agent_id(agent_id: str) -> str:
    return f"urn:bernstein:agent:{agent_id}"


def _model_agent_id(provider: str, model_requested: str) -> str:
    return f"urn:bernstein:agent:model:{provider}:{model_requested}"


def _iso_from_ts_ns(ts_ns: int) -> str:
    """Return an xsd:dateTime string for a nanosecond epoch timestamp.

    Built from integer division alone (no float arithmetic) so the same
    ``ts_ns`` always renders to the same string regardless of platform. The
    nanosecond remainder is kept as a 9-digit fractional-seconds suffix
    instead of being rounded away, so the export loses no precision the
    entry carried.
    """
    seconds, nanos = divmod(ts_ns, 1_000_000_000)
    dt = datetime.fromtimestamp(seconds, tz=UTC)
    return dt.strftime("%Y-%m-%dT%H:%M:%S") + f".{nanos:09d}Z"


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ProvEntity:
    """One ``prov:Entity``: an artefact version or an attached input."""

    id: str
    kind: str  # "artefact" | "attachment"
    content_hash: str
    artefact_path: str = ""
    trust_class: str | None = None
    sensitivity: str | None = None
    generated_at_time: str = ""


@dataclass(frozen=True, slots=True)
class ProvActivity:
    """One ``prov:Activity``: the turn that produced an entity."""

    id: str
    entry_hash: str
    tool_call_id: str
    span_id: str
    ended_at_time: str


@dataclass(frozen=True, slots=True)
class ProvAgent:
    """One ``prov:Agent``: the operator agent, or a model reference (#5037)."""

    id: str
    kind: str  # "operator" | "model"
    label: str


@dataclass(frozen=True, slots=True)
class ProvRelation:
    """One PROV relation between two of the ids above."""

    kind: str  # "wasGeneratedBy" | "wasAssociatedWith" | "wasDerivedFrom" | "used"
    subject: str
    obj: str
    role: str = ""


@dataclass(slots=True)
class ProvDocument:
    """A PROV-O projection of one artefact's ancestry.

    ``entities``/``activities``/``agents``/``relations`` are each sorted by
    a stable key so two projections of the same ancestry -- regardless of
    the order the source entries were supplied in -- produce the same
    document.
    """

    schema_version: str
    root_entity_id: str
    entities: list[ProvEntity] = field(default_factory=list[ProvEntity])
    activities: list[ProvActivity] = field(default_factory=list[ProvActivity])
    agents: list[ProvAgent] = field(default_factory=list[ProvAgent])
    relations: list[ProvRelation] = field(default_factory=list[ProvRelation])


# ---------------------------------------------------------------------------
# Ancestry closure
# ---------------------------------------------------------------------------


def _ancestry_closure(index: dict[str, LineageEntry], start_hash: str) -> list[str]:
    """Return entry hashes reachable from ``start_hash`` via ``parent_hashes``.

    A plain reachability walk over a set, so the result does not depend on
    traversal order; sorted before return so two calls over the same index
    return the identical list regardless of ``dict``/``set`` iteration order.
    An entry named in ``parent_hashes`` but absent from ``index`` marks the
    edge of the supplied window and is not followed further.
    """
    seen: set[str] = set()
    frontier = [start_hash]
    while frontier:
        h = frontier.pop()
        if h in seen:
            continue
        seen.add(h)
        entry = index.get(h)
        if entry is None:
            continue
        frontier.extend(entry.parent_hashes)
    return sorted(seen)


# ---------------------------------------------------------------------------
# Projection
# ---------------------------------------------------------------------------


def project_prov_ancestry(entries: Sequence[LineageEntry], *, root_entry_hash: str) -> ProvDocument:
    """Project the ancestry of one lineage entry into a PROV-O document.

    Mapping (see issue #5039):

    * artefact (``content_hash``, ``artefact_path``) -> ``prov:Entity``
    * producing turn (``tool_call_id``, ``span_id``) -> ``prov:Activity``
    * ``agent_id`` / ``agent_card_kid`` -> ``prov:Agent``,
      ``prov:wasAssociatedWith``
    * ``parent_hashes`` -> ``prov:wasDerivedFrom``
    * ``attachment_digests`` -> ``prov:used``
    * ``ts_ns`` -> ``prov:generatedAtTime`` / ``prov:endTime``
    * ``model_ref`` -> a second ``prov:Agent``, ``prov:wasAssociatedWith``
    * ``trust_class`` / ``sensitivity`` -> qualified ``bernstein:`` attributes

    Args:
        entries: Lineage entries to search. Only the transitive closure of
            ``root_entry_hash`` over ``parent_hashes`` is exported; entries
            outside that closure (other artefacts, other branches) are
            ignored.
        root_entry_hash: The entry hash naming the artefact version whose
            ancestry is exported (e.g. the open tip of an artefact path,
            from :func:`bernstein.core.lineage.tips.compute_tips`).

    Returns:
        A :class:`ProvDocument`. Pass it to :func:`to_prov_json` or
        :func:`to_turtle`.

    Raises:
        ProvExportError: When ``root_entry_hash`` is not among ``entries``.
            The export is unproducible without it, not merely empty.
    """
    index: dict[str, LineageEntry] = {entry_hash(e): e for e in entries}
    if root_entry_hash not in index:
        msg = f"no lineage entry with hash {root_entry_hash!r}; PROV export is unproducible without it"
        raise ProvExportError(msg)

    closure = _ancestry_closure(index, root_entry_hash)

    entities: dict[str, ProvEntity] = {}
    activities: dict[str, ProvActivity] = {}
    agents: dict[str, ProvAgent] = {}
    relations: list[ProvRelation] = []

    for h in closure:
        entry = index[h]
        eid = _entity_id(entry.content_hash)
        gen_time = _iso_from_ts_ns(entry.ts_ns)
        if eid not in entities:
            entities[eid] = ProvEntity(
                id=eid,
                kind="artefact",
                content_hash=entry.content_hash,
                artefact_path=entry.artefact_path,
                trust_class=entry.trust_class,
                sensitivity=entry.sensitivity,
                generated_at_time=gen_time,
            )

        aid = _activity_id(h)
        activities[aid] = ProvActivity(
            id=aid,
            entry_hash=h,
            tool_call_id=entry.tool_call_id,
            span_id=entry.span_id,
            ended_at_time=gen_time,
        )
        relations.append(ProvRelation(kind="wasGeneratedBy", subject=eid, obj=aid))

        agid = _agent_id(entry.agent_id)
        agents.setdefault(agid, ProvAgent(id=agid, kind="operator", label=entry.agent_id))
        relations.append(ProvRelation(kind="wasAssociatedWith", subject=aid, obj=agid, role="operator"))

        if entry.model_ref is not None:
            mid = _model_agent_id(entry.model_ref.provider, entry.model_ref.model_requested)
            agents.setdefault(mid, ProvAgent(id=mid, kind="model", label=entry.model_ref.model_requested))
            relations.append(ProvRelation(kind="wasAssociatedWith", subject=aid, obj=mid, role="model"))

        for parent_hash in entry.parent_hashes:
            parent_entry = index.get(parent_hash)
            if parent_entry is None:
                continue
            peid = _entity_id(parent_entry.content_hash)
            relations.append(ProvRelation(kind="wasDerivedFrom", subject=eid, obj=peid))

        for digest in entry.attachment_digests or ():
            ateid = _attachment_entity_id(digest)
            entities.setdefault(
                ateid,
                ProvEntity(id=ateid, kind="attachment", content_hash=f"sha256:{digest}"),
            )
            relations.append(ProvRelation(kind="used", subject=aid, obj=ateid))

    root_entity_id = _entity_id(index[root_entry_hash].content_hash)
    ordered_relations = sorted(set(relations), key=lambda r: (r.kind, r.subject, r.obj, r.role))

    return ProvDocument(
        schema_version=PROV_SCHEMA_VERSION,
        root_entity_id=root_entity_id,
        entities=sorted(entities.values(), key=lambda e: e.id),
        activities=sorted(activities.values(), key=lambda a: a.id),
        agents=sorted(agents.values(), key=lambda a: a.id),
        relations=ordered_relations,
    )


# ---------------------------------------------------------------------------
# PROV-JSON serialisation
# ---------------------------------------------------------------------------


def _entity_attrs(e: ProvEntity) -> dict[str, Any]:
    attrs: dict[str, Any] = {
        "prov:type": f"bernstein:{e.kind}",
        "bernstein:contentHash": e.content_hash,
    }
    if e.artefact_path:
        attrs["bernstein:artefactPath"] = e.artefact_path
    if e.generated_at_time:
        attrs["prov:generatedAtTime"] = e.generated_at_time
    if e.trust_class is not None:
        attrs["bernstein:trustClass"] = e.trust_class
    if e.sensitivity is not None:
        attrs["bernstein:sensitivity"] = e.sensitivity
    return attrs


def _activity_attrs(a: ProvActivity) -> dict[str, Any]:
    attrs: dict[str, Any] = {"bernstein:entryHash": a.entry_hash, "prov:endTime": a.ended_at_time}
    if a.tool_call_id:
        attrs["bernstein:toolCallId"] = a.tool_call_id
    if a.span_id:
        attrs["bernstein:spanId"] = a.span_id
    return attrs


def _agent_attrs(a: ProvAgent) -> dict[str, Any]:
    return {"prov:type": f"bernstein:{a.kind}", "bernstein:label": a.label}


def to_prov_json(doc: ProvDocument) -> dict[str, Any]:
    """Render ``doc`` as a PROV-JSON document (W3C PROV-JSON).

    Relation ids are ``_:<kind><index>`` over the document's already-sorted
    relation list -- a stable position, not a generated identifier, so two
    projections of the same ancestry emit the same ids.
    """
    entity = {e.id: _entity_attrs(e) for e in doc.entities}
    activity = {a.id: _activity_attrs(a) for a in doc.activities}
    agent = {a.id: _agent_attrs(a) for a in doc.agents}

    was_generated_by: dict[str, Any] = {}
    used: dict[str, Any] = {}
    was_associated_with: dict[str, Any] = {}
    was_derived_from: dict[str, Any] = {}

    for index, r in enumerate(doc.relations):
        rid = f"_:{r.kind}{index}"
        if r.kind == "wasGeneratedBy":
            was_generated_by[rid] = {"prov:entity": r.subject, "prov:activity": r.obj}
        elif r.kind == "used":
            used[rid] = {"prov:activity": r.subject, "prov:entity": r.obj}
        elif r.kind == "wasAssociatedWith":
            body: dict[str, Any] = {"prov:activity": r.subject, "prov:agent": r.obj}
            if r.role:
                body["prov:role"] = r.role
            was_associated_with[rid] = body
        elif r.kind == "wasDerivedFrom":
            was_derived_from[rid] = {"prov:generatedEntity": r.subject, "prov:usedEntity": r.obj}

    payload: dict[str, Any] = {
        "schema_version": doc.schema_version,
        "prefix": {"prov": NS_PROV, "xsd": NS_XSD, "bernstein": NS_BERNSTEIN},
        "root_entity": doc.root_entity_id,
        "entity": entity,
        "activity": activity,
        "agent": agent,
    }
    if was_generated_by:
        payload["wasGeneratedBy"] = was_generated_by
    if used:
        payload["used"] = used
    if was_associated_with:
        payload["wasAssociatedWith"] = was_associated_with
    if was_derived_from:
        payload["wasDerivedFrom"] = was_derived_from
    return payload


def canonical_prov_json_bytes(doc: ProvDocument) -> bytes:
    """Return deterministic PROV-JSON bytes: sorted keys, compact, UTF-8."""
    return json.dumps(to_prov_json(doc), sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


# ---------------------------------------------------------------------------
# Turtle serialisation (slice 2, from the same ProvDocument intermediate)
# ---------------------------------------------------------------------------


def _turtle_literal(value: str) -> str:
    """Escape a string for a Turtle quoted literal."""
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _turtle_uri(value: str) -> str:
    """Wrap a URI in angle brackets, escaping the characters Turtle forbids raw."""
    return "<" + value.replace("\\", "%5C").replace(">", "%3E").replace(" ", "%20") + ">"


def _turtle_block(subject: str, type_: str, predicates: list[tuple[str, str]]) -> str:
    """Render one Turtle subject block: ``<subject> a type_ ; p1 o1 ; ... .``"""
    lines = [f"{_turtle_uri(subject)} a {type_} ;"]
    for i, (pred, obj) in enumerate(predicates):
        suffix = " ." if i == len(predicates) - 1 else " ;"
        lines.append(f"    {pred} {obj}{suffix}")
    return "\n".join(lines)


_RELATION_PREDICATE: dict[str, str] = {
    "wasGeneratedBy": "prov:wasGeneratedBy",
    "used": "prov:used",
    "wasAssociatedWith": "prov:wasAssociatedWith",
    "wasDerivedFrom": "prov:wasDerivedFrom",
}


def to_turtle(doc: ProvDocument) -> str:
    """Render ``doc`` as Turtle, built from the same :class:`ProvDocument`
    that :func:`to_prov_json` renders -- the two never diverge on what the
    ancestry contains, only on syntax.
    """
    lines: list[str] = [
        f"@prefix prov: <{NS_PROV}> .",
        f"@prefix xsd: <{NS_XSD}> .",
        f"@prefix bernstein: <{NS_BERNSTEIN}> .",
        "",
    ]

    for e in doc.entities:
        preds: list[tuple[str, str]] = [("bernstein:contentHash", f'"{_turtle_literal(e.content_hash)}"')]
        if e.artefact_path:
            preds.append(("bernstein:artefactPath", f'"{_turtle_literal(e.artefact_path)}"'))
        if e.generated_at_time:
            preds.append(("prov:generatedAtTime", f'"{e.generated_at_time}"^^xsd:dateTime'))
        if e.trust_class is not None:
            preds.append(("bernstein:trustClass", f'"{_turtle_literal(e.trust_class)}"'))
        if e.sensitivity is not None:
            preds.append(("bernstein:sensitivity", f'"{_turtle_literal(e.sensitivity)}"'))
        lines.append(_turtle_block(e.id, "prov:Entity", preds))
        lines.append("")

    for a in doc.activities:
        preds = [("bernstein:entryHash", f'"{_turtle_literal(a.entry_hash)}"')]
        if a.tool_call_id:
            preds.append(("bernstein:toolCallId", f'"{_turtle_literal(a.tool_call_id)}"'))
        if a.span_id:
            preds.append(("bernstein:spanId", f'"{_turtle_literal(a.span_id)}"'))
        preds.append(("prov:endTime", f'"{a.ended_at_time}"^^xsd:dateTime'))
        lines.append(_turtle_block(a.id, "prov:Activity", preds))
        lines.append("")

    for ag in doc.agents:
        lines.append(_turtle_block(ag.id, "prov:Agent", [("bernstein:label", f'"{_turtle_literal(ag.label)}"')]))
        lines.append("")

    for r in doc.relations:
        predicate = _RELATION_PREDICATE[r.kind]
        lines.append(f"{_turtle_uri(r.subject)} {predicate} {_turtle_uri(r.obj)} .")

    return "\n".join(lines).rstrip("\n") + "\n"
