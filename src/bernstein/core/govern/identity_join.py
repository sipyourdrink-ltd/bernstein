"""Cross-source inventory identity: field maps, join, profile, entity store.

Two collectors describing the same host disagree about everything except the
host: they name their columns differently, one carries a field the other has
never heard of, and the same hardware id arrives punctuated one way here and
another way there. Joining them needs four declared things, and this module
holds all four.

Declared field maps
    :class:`FieldMapTable` maps each source's own column names onto canonical
    field names. A source that does not carry a canonical field maps it to
    ``None`` — absence is declared, never inferred. Every projected value
    therefore arrives with a presence marker: :data:`PRESENT`,
    :data:`MISSING` (the source has the column, this record left it empty) or
    :data:`DECLARED_ABSENT` (the source has no such column at all). Without
    that marker the two nulls are indistinguishable, and a silently failed
    join reads exactly like a field the source never had.

Normalised join keys
    Each canonical field declares a normaliser, and normalisation runs on
    projection — before any matching. ``ab-12-cd`` from one source and
    ``AB12CD`` from another are one entity, not two.

Nodes and edges
    :func:`join_sources` returns an :class:`IdentityGraph`, never a flat
    frame. Every source's value for a field is kept under that source, so two
    sources disagreeing about a value is recorded, not resolved. Fields that
    declare an ``edge_kind`` produce edges, and both endpoints are entity ids.

Per-source quality profile
    Each pass emits one :class:`SourceProfile` per source (rows, columns,
    per-field null counts, ``observed_at``). Appending rather than
    overwriting is what makes a source going quietly stale visible as a diff
    between two passes.

Store layout
    :class:`EntityStore` writes one JSON file per entity keyed by its stable
    id, enumerations into lookup files, and validates every file it loads
    against a schema that requires ``observed_at`` and rejects anything but an
    entity id where an edge endpoint belongs.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from jsonschema import Draft202012Validator
from jsonschema import ValidationError as JsonSchemaValidationError

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

#: The source carries this field and this record supplied a value.
PRESENT = "present"
#: The source carries this field; this record supplied no value.
MISSING = "missing"
#: The source declares it carries no such field at all.
DECLARED_ABSENT = "declared_absent"

_ENTITY_ID_PATTERN = "^entity:[0-9a-f]{32}$"


class FieldMapError(ValueError):
    """Raised when a field-mapping table is not fully declared."""


class IdentityJoinError(ValueError):
    """Raised when a record cannot be joined."""


class EntityStoreError(ValueError):
    """Raised when an entity file fails schema validation."""


def normalise_hardware_id(value: str) -> str:
    """Normalise a hardware id: alphanumerics only, upper-cased.

    Serial numbers arrive punctuated differently per collector
    (``ab-12-cd``, ``AB12CD``, ``ab 12 cd``); all three name one host.
    """
    return "".join(ch for ch in value if ch.isalnum()).upper()


def normalise_account_name(value: str) -> str:
    """Normalise an account name: trimmed and lower-cased."""
    return value.strip().lower()


def normalise_identity(value: str) -> str:
    """Leave a value as it was observed."""
    return value


_NORMALISERS: dict[str, Any] = {
    "none": normalise_identity,
    "hardware_id": normalise_hardware_id,
    "account_name": normalise_account_name,
}


@dataclass(frozen=True, slots=True)
class CanonicalField:
    """One canonical field every source is mapped onto.

    Attributes:
        name: The canonical field name.
        normaliser: Name of the normaliser applied on projection, before any
            join. One of ``none``, ``hardware_id``, ``account_name``.
        edge_kind: When set, a present value produces an edge of this kind
            from the record's entity to the entity the value names.
    """

    name: str
    normaliser: str = "none"
    edge_kind: str | None = None

    def __post_init__(self) -> None:
        if self.normaliser not in _NORMALISERS:
            raise FieldMapError(f"unknown normaliser {self.normaliser!r} for field {self.name!r}")

    def normalise(self, value: str) -> str:
        """Apply this field's declared normaliser to *value*."""
        normaliser = _NORMALISERS[self.normaliser]
        return str(normaliser(value))


@dataclass(frozen=True, slots=True)
class SourceFieldMap:
    """How one source names each canonical field.

    Attributes:
        source: The source identifier (collector or feed name).
        fields: Canonical field name to this source's own field name, or
            ``None`` when the source carries no such field.
    """

    source: str
    fields: Mapping[str, str | None]

    def column_for(self, canonical: str) -> str | None:
        """Return this source's column name for *canonical*, or ``None``."""
        return self.fields[canonical]


@dataclass(frozen=True, slots=True)
class SourceValue:
    """One source's value for one canonical field of one entity.

    Attributes:
        source: Which source supplied (or declined to supply) the value.
        field: The canonical field name.
        value: The normalised value, or ``None`` when not present.
        presence: :data:`PRESENT`, :data:`MISSING` or :data:`DECLARED_ABSENT`.
    """

    source: str
    field: str
    value: str | None
    presence: str

    def to_dict(self) -> dict[str, Any]:
        """Return the canonical serialization."""
        return {
            "source": self.source,
            "field": self.field,
            "value": self.value,
            "presence": self.presence,
        }


@dataclass(frozen=True, slots=True)
class FieldMapTable:
    """The declared mapping from every source's fields onto canonical names.

    Attributes:
        fields: The canonical fields, in declaration order.
        key: The canonical field records join on.
        sources: Source identifier to that source's field map. Every source
            map must name every canonical field, mapping the ones it does not
            carry to ``None``.
    """

    fields: tuple[CanonicalField, ...]
    key: str
    sources: Mapping[str, SourceFieldMap]

    def __post_init__(self) -> None:
        names = [f.name for f in self.fields]
        if len(names) != len(set(names)):
            raise FieldMapError("canonical field names must be unique")
        canonical = set(names)
        if self.key not in canonical:
            raise FieldMapError(f"key field {self.key!r} is not a canonical field")
        for source, source_map in self.sources.items():
            if source_map.source != source:
                raise FieldMapError(f"source map keyed {source!r} declares source {source_map.source!r}")
            declared = set(source_map.fields)
            undeclared = canonical - declared
            if undeclared:
                raise FieldMapError(
                    f"source {source!r} does not declare canonical field(s) "
                    f"{sorted(undeclared)}: absence must be declared, not inferred"
                )
            unknown = declared - canonical
            if unknown:
                raise FieldMapError(f"source {source!r} maps unknown canonical field(s) {sorted(unknown)}")

    def field(self, name: str) -> CanonicalField:
        """Return the canonical field declaration named *name*."""
        for candidate in self.fields:
            if candidate.name == name:
                return candidate
        raise FieldMapError(f"unknown canonical field {name!r}")

    def project(self, source: str, record: Mapping[str, Any]) -> tuple[SourceValue, ...]:
        """Project one raw *record* from *source* onto canonical fields.

        Normalisation runs here, before the join, so callers cannot match on
        un-normalised values by accident.
        """
        try:
            source_map = self.sources[source]
        except KeyError:
            raise IdentityJoinError(f"no field map declared for source {source!r}") from None

        projected: list[SourceValue] = []
        for canonical in self.fields:
            column = source_map.column_for(canonical.name)
            if column is None:
                projected.append(
                    SourceValue(
                        source=source,
                        field=canonical.name,
                        value=None,
                        presence=DECLARED_ABSENT,
                    )
                )
                continue
            raw = record.get(column)
            if raw is None or str(raw).strip() == "":
                projected.append(
                    SourceValue(
                        source=source,
                        field=canonical.name,
                        value=None,
                        presence=MISSING,
                    )
                )
                continue
            projected.append(
                SourceValue(
                    source=source,
                    field=canonical.name,
                    value=canonical.normalise(str(raw)),
                    presence=PRESENT,
                )
            )
        return tuple(projected)


def entity_id_for(key_field: str, normalised_value: str) -> str:
    """Return the stable entity id for *normalised_value* in *key_field*.

    The id is a pure function of the key domain and the normalised value, so
    two passes over the same environment name the same entity identically.
    """
    digest = hashlib.sha256(f"{key_field}\x1f{normalised_value}".encode()).hexdigest()
    return f"entity:{digest[:32]}"


@dataclass(frozen=True, slots=True)
class EntityEdge:
    """A relation between two entities, both named by id.

    Attributes:
        edge_id: Stable id of this edge.
        kind: The relation kind, declared by the field that produced it.
        from_id: Entity id of the record the edge was observed on.
        to_id: Entity id the field's value names.
        source: The source that observed the relation.
        observed_at: When the pass that produced this edge ran.
    """

    edge_id: str
    kind: str
    from_id: str
    to_id: str
    source: str
    observed_at: str

    def to_dict(self) -> dict[str, Any]:
        """Return the canonical serialization, endpoints as ids."""
        return {
            "edge_id": self.edge_id,
            "kind": self.kind,
            "from": self.from_id,
            "to": self.to_id,
            "source": self.source,
            "observed_at": self.observed_at,
        }


@dataclass(frozen=True, slots=True)
class EntityNode:
    """One entity, carrying every source's view of it.

    Attributes:
        entity_id: Stable id derived from the normalised key.
        key_field: The canonical field the entity is keyed on.
        key: The normalised key value.
        observed_at: When the pass that produced this node ran.
        attributes: Every source's value for every canonical field, each with
            its presence marker. Sources disagreeing is recorded, not resolved.
        edges: Relations observed on this entity, endpoints as ids.
    """

    entity_id: str
    key_field: str
    key: str
    observed_at: str
    attributes: tuple[SourceValue, ...]
    edges: tuple[EntityEdge, ...]

    def to_dict(self) -> dict[str, Any]:
        """Return the canonical serialization."""
        return {
            "entity_id": self.entity_id,
            "key_field": self.key_field,
            "key": self.key,
            "observed_at": self.observed_at,
            "attributes": [a.to_dict() for a in self.attributes],
            "edges": [e.to_dict() for e in self.edges],
        }


@dataclass(frozen=True, slots=True)
class IdentityGraph:
    """The joined store: nodes and edges, never a flat frame."""

    nodes: tuple[EntityNode, ...]
    edges: tuple[EntityEdge, ...]

    def node(self, entity_id: str) -> EntityNode | None:
        """Look up a node by entity id."""
        for candidate in self.nodes:
            if candidate.entity_id == entity_id:
                return candidate
        return None


@dataclass(frozen=True, slots=True)
class SourceProfile:
    """One source's quality profile for one pass.

    Attributes:
        source: The profiled source.
        pass_id: Identifier of the pass that produced this profile.
        rows: How many records the source supplied.
        columns: Canonical fields the source declares it carries.
        null_counts: Canonical field to how many records had no value for it,
            counting declared-absent fields as null in every record.
        observed_at: When the pass ran.
    """

    source: str
    pass_id: str
    rows: int
    columns: tuple[str, ...]
    null_counts: Mapping[str, int]
    observed_at: str

    def to_dict(self) -> dict[str, Any]:
        """Return the canonical serialization."""
        return {
            "source": self.source,
            "pass_id": self.pass_id,
            "rows": self.rows,
            "columns": list(self.columns),
            "null_counts": dict(self.null_counts),
            "observed_at": self.observed_at,
        }


@dataclass(frozen=True, slots=True)
class JoinResult:
    """What one pass produced: the graph and each source's profile."""

    graph: IdentityGraph
    profiles: tuple[SourceProfile, ...]


def join_sources(
    *,
    table: FieldMapTable,
    records: Mapping[str, Sequence[Mapping[str, Any]]],
    observed_at: str,
    pass_id: str,
) -> JoinResult:
    """Join every source's *records* into one identity graph.

    Records are projected through *table* (normalising as they go), grouped by
    their normalised key into nodes, and every source's value is kept under
    that source. Fields declaring an ``edge_kind`` produce edges whose
    endpoints are entity ids.

    Determinism: nodes, attributes and edges are sorted, so two operators
    running the same pass over the same records produce identical output.

    Raises:
        IdentityJoinError: A record supplied no value for the key field, so
            there is no entity for it to belong to.
    """
    by_key: dict[str, list[SourceValue]] = {}
    edges_by_key: dict[str, list[EntityEdge]] = {}
    profiles: list[SourceProfile] = []

    for source in sorted(records):
        source_map = table.sources.get(source)
        if source_map is None:
            raise IdentityJoinError(f"no field map declared for source {source!r}")
        rows = list(records[source])
        null_counts = {f.name: 0 for f in table.fields}

        for record in rows:
            projected = table.project(source, record)
            values = {v.field: v for v in projected}
            for value in projected:
                if value.presence != PRESENT:
                    null_counts[value.field] += 1

            key_value = values[table.key]
            if key_value.presence != PRESENT or key_value.value is None:
                raise IdentityJoinError(
                    f"source {source!r} supplied a record with no {table.key!r} value "
                    f"({key_value.presence}); it cannot be joined to an entity"
                )
            key = key_value.value
            entity = entity_id_for(table.key, key)
            by_key.setdefault(key, []).extend(projected)

            for canonical in table.fields:
                if canonical.edge_kind is None:
                    continue
                value = values[canonical.name]
                if value.presence != PRESENT or value.value is None:
                    continue
                to_id = entity_id_for(canonical.name, value.value)
                edges_by_key.setdefault(key, []).append(
                    EntityEdge(
                        edge_id=_edge_id(entity, canonical.edge_kind, to_id, source),
                        kind=canonical.edge_kind,
                        from_id=entity,
                        to_id=to_id,
                        source=source,
                        observed_at=observed_at,
                    )
                )

        profiles.append(
            SourceProfile(
                source=source,
                pass_id=pass_id,
                rows=len(rows),
                columns=tuple(f.name for f in table.fields if source_map.column_for(f.name) is not None),
                null_counts=null_counts,
                observed_at=observed_at,
            )
        )

    nodes: list[EntityNode] = []
    all_edges: list[EntityEdge] = []
    for key in sorted(by_key):
        entity = entity_id_for(table.key, key)
        node_edges = tuple(sorted(edges_by_key.get(key, []), key=lambda e: e.edge_id))
        nodes.append(
            EntityNode(
                entity_id=entity,
                key_field=table.key,
                key=key,
                observed_at=observed_at,
                attributes=tuple(sorted(by_key[key], key=lambda v: (v.field, v.source, v.value or ""))),
                edges=node_edges,
            )
        )
        all_edges.extend(node_edges)

    return JoinResult(
        graph=IdentityGraph(nodes=tuple(nodes), edges=tuple(all_edges)),
        profiles=tuple(profiles),
    )


def _edge_id(from_id: str, kind: str, to_id: str, source: str) -> str:
    payload = "\x1f".join((from_id, kind, to_id, source))
    return "edge:" + hashlib.sha256(payload.encode()).hexdigest()[:32]


ENTITY_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "additionalProperties": False,
    "required": ["entity_id", "key_field", "key", "observed_at", "attributes", "edges"],
    "properties": {
        "entity_id": {"type": "string", "pattern": _ENTITY_ID_PATTERN},
        "key_field": {"type": "string", "minLength": 1},
        "key": {"type": "string", "minLength": 1},
        "observed_at": {"type": "string", "minLength": 1},
        "attributes": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["source", "field", "value", "presence"],
                "properties": {
                    "source": {"type": "string", "minLength": 1},
                    "field": {"type": "string", "minLength": 1},
                    "value": {"type": ["string", "null"]},
                    "presence": {"enum": [PRESENT, MISSING, DECLARED_ABSENT]},
                },
            },
        },
        "edges": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["edge_id", "kind", "from", "to", "source", "observed_at"],
                "properties": {
                    "edge_id": {"type": "string", "pattern": "^edge:[0-9a-f]{32}$"},
                    "kind": {"type": "string", "minLength": 1},
                    "from": {"type": "string", "pattern": _ENTITY_ID_PATTERN},
                    "to": {"type": "string", "pattern": _ENTITY_ID_PATTERN},
                    "source": {"type": "string", "minLength": 1},
                    "observed_at": {"type": "string", "minLength": 1},
                },
            },
        },
    },
}

_ENTITY_VALIDATOR: Any = Draft202012Validator(ENTITY_SCHEMA)


def store_root(workdir: Path) -> Path:
    """Return the inventory store root for *workdir*.

    The store sits under ``.sdd/govern/`` beside the lineage spine, so every
    governance artifact stays under one root.
    """
    return Path(workdir) / ".sdd" / "govern" / "inventory"


@dataclass(frozen=True, slots=True)
class EntityStore:
    """One JSON file per entity, plus lookup files and a profile log.

    Attributes:
        root: Directory holding ``entities/``, ``lookups/`` and
            ``profiles.jsonl``.
    """

    root: Path

    def entity_path(self, entity_id: str) -> Path:
        """Return the path of the file holding *entity_id*."""
        if not entity_id.startswith("entity:"):
            raise EntityStoreError(f"not an entity id: {entity_id!r}")
        return self.root / "entities" / f"{entity_id.removeprefix('entity:')}.json"

    def write_graph(self, graph: IdentityGraph) -> None:
        """Write *graph* as one file per entity plus lookup files."""
        entities = self.root / "entities"
        entities.mkdir(parents=True, exist_ok=True)
        for node in graph.nodes:
            payload = node.to_dict()
            self._validate(payload, node.entity_id)
            _write_json(self.entity_path(node.entity_id), payload)

        lookups = self.root / "lookups"
        lookups.mkdir(parents=True, exist_ok=True)
        sources = sorted({a.source for n in graph.nodes for a in n.attributes} | {e.source for e in graph.edges})
        _write_json(lookups / "sources.json", sources)
        _write_json(lookups / "edge_kinds.json", sorted({e.kind for e in graph.edges}))
        _write_json(
            lookups / "canonical_fields.json",
            sorted({a.field for n in graph.nodes for a in n.attributes}),
        )

    def load_entity(self, entity_id: str) -> dict[str, Any]:
        """Load and validate one entity file.

        Raises:
            EntityStoreError: The file is absent, is not JSON, or does not
                satisfy :data:`ENTITY_SCHEMA`.
        """
        path = self.entity_path(entity_id)
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise EntityStoreError(f"cannot read entity {entity_id}: {exc}") from exc
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise EntityStoreError(f"entity {entity_id} is not valid JSON: {exc}") from exc
        self._validate(payload, entity_id)
        return dict(payload)

    def entity_ids(self) -> tuple[str, ...]:
        """Return every entity id in the store, sorted."""
        entities = self.root / "entities"
        if not entities.is_dir():
            return ()
        return tuple(sorted(f"entity:{p.stem}" for p in entities.glob("*.json")))

    def append_profiles(self, profiles: Sequence[SourceProfile]) -> None:
        """Append this pass's *profiles* to the log, never overwriting."""
        self.root.mkdir(parents=True, exist_ok=True)
        with (self.root / "profiles.jsonl").open("a", encoding="utf-8") as handle:
            for profile in profiles:
                handle.write(
                    json.dumps(
                        profile.to_dict(),
                        ensure_ascii=False,
                        separators=(",", ":"),
                        sort_keys=True,
                    )
                    + "\n"
                )

    def load_profiles(self) -> tuple[dict[str, Any], ...]:
        """Return every profile recorded so far, in the order recorded."""
        path = self.root / "profiles.jsonl"
        if not path.is_file():
            return ()
        return tuple(json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip())

    def _validate(self, payload: Any, entity_id: str) -> None:
        raw = cast("list[JsonSchemaValidationError]", list(_ENTITY_VALIDATOR.iter_errors(payload)))
        errors = sorted(raw, key=lambda e: list(e.path))
        if errors:
            first = errors[0]
            location = "/".join(str(p) for p in first.path) or "<root>"
            raise EntityStoreError(f"entity {entity_id} invalid at {location}: {first.message}")


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


__all__ = [
    "DECLARED_ABSENT",
    "ENTITY_SCHEMA",
    "MISSING",
    "PRESENT",
    "CanonicalField",
    "EntityEdge",
    "EntityNode",
    "EntityStore",
    "EntityStoreError",
    "FieldMapError",
    "FieldMapTable",
    "IdentityGraph",
    "IdentityJoinError",
    "JoinResult",
    "SourceFieldMap",
    "SourceProfile",
    "SourceValue",
    "entity_id_for",
    "join_sources",
    "normalise_account_name",
    "normalise_hardware_id",
    "normalise_identity",
    "store_root",
]
