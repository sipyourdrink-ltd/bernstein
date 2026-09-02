"""Selector grammar and typed resolution API for the govern inventory.

A selector is a chain of ``key value`` filters over the attributes of the
governed inventory. Every consumer that needs a target subset -- an audit pass,
a scheduler, a reconcile lane -- resolves it through :func:`resolve_targets`,
so "what does this selector actually match" is answered by running it rather
than by reading one ad hoc filter implementation per consumer.

Grammar (tokens, never a shell string)::

    kind bucket region ~^us- group {prod,staging}

* ``key value``      -- exact match on any value of the attribute
* ``key ~regex``     -- :mod:`re` search against any value
* ``key {a,b,c}``    -- membership in a declared set
* ``key \\literal``   -- exact match, escaping a leading ``~``, ``{`` or ``\\``
* ``@alias``         -- expands to the filter pairs declared for it as data

Two attribute keys are reserved and always present on a resolved node: ``id``
(the node identifier) and ``group`` (its group memberships), so a selector can
name a group without the store needing a matching attribute.

Group membership is an edge. Values attached to a group resolve onto its
members; a value declared on the node itself overrides the group's. Two groups
carrying different values for the same key, with no node-level value to settle
it, are a :class:`GroupConflictError` rather than a silent precedence rule --
a precedence rule would make the match set readable only from this module's
source, which is the failure mode this API exists to remove.

Ordering is by node identifier, so two operators running the same selector
against the same store get the same result to diff or script against.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence
    from pathlib import Path

#: Attribute keys the resolver always fills in, shadowing any stored attribute.
RESERVED_KEYS = ("id", "group")

_FIELD_RE = re.compile(r"%([A-Za-z_][A-Za-z0-9_.\-]*)")


class SelectorError(Exception):
    """Base class for every selector failure."""


class SelectorSyntaxError(SelectorError):
    """A selector, alias table or projection template is malformed."""


class GroupConflictError(SelectorError):
    """Two groups carry different values for one key and no node value settles it."""


def _freeze(values: Mapping[str, Sequence[str]]) -> Mapping[str, tuple[str, ...]]:
    """Return an immutable view of *values* with tuple-valued entries."""
    return MappingProxyType({str(k): tuple(str(v) for v in vs) for k, vs in values.items()})


@dataclass(frozen=True, slots=True)
class InventoryNode:
    """One selectable entity in the governed inventory.

    Attributes:
        node_id: Stable identifier of the node (ARN, repo name, path).
        attributes: Discovered attributes; every attribute is multi-valued.
        groups: Group identifiers this node is an edge to.
    """

    node_id: str
    attributes: Mapping[str, tuple[str, ...]] = field(default_factory=dict[str, tuple[str, ...]])
    groups: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "attributes", _freeze(self.attributes))
        object.__setattr__(self, "groups", tuple(self.groups))


@dataclass(frozen=True, slots=True)
class InventoryGroup:
    """Values attached to a group, inherited by its members.

    Attributes:
        group_id: Stable identifier of the group.
        values: Attribute values every member inherits unless it declares its own.
    """

    group_id: str
    values: Mapping[str, tuple[str, ...]] = field(default_factory=dict[str, tuple[str, ...]])

    def __post_init__(self) -> None:
        object.__setattr__(self, "values", _freeze(self.values))


@dataclass(frozen=True, slots=True)
class InventoryStore:
    """The nodes and groups a selector queries.

    Attributes:
        nodes: Every discovered node. Declaration order does not affect results.
        groups: Every declared group.
    """

    nodes: tuple[InventoryNode, ...] = ()
    groups: tuple[InventoryGroup, ...] = ()

    def group(self, group_id: str) -> InventoryGroup | None:
        """Look up a group by identifier."""
        for g in self.groups:
            if g.group_id == group_id:
                return g
        return None


@dataclass(frozen=True, slots=True)
class ResolvedNode:
    """A node with its group inheritance already applied.

    Attributes:
        node_id: Stable identifier of the node.
        attributes: Effective attributes, including the reserved keys.
    """

    node_id: str
    attributes: Mapping[str, tuple[str, ...]]

    def values(self, key: str) -> tuple[str, ...]:
        """Return the effective values for *key*, empty when it is not set."""
        return self.attributes.get(key, ())


class FilterKind(StrEnum):
    """How a filter compares its value against an attribute."""

    EXACT = "exact"
    REGEX = "regex"
    SET = "set"


@dataclass(frozen=True, slots=True)
class Filter:
    """One ``key value`` term of a selector.

    Attributes:
        key: Attribute key the term applies to.
        kind: Comparison performed against the attribute's values.
        literal: The exact value, for :attr:`FilterKind.EXACT`.
        members: The declared set, for :attr:`FilterKind.SET`.
        pattern: The compiled expression, for :attr:`FilterKind.REGEX`.
    """

    key: str
    kind: FilterKind
    literal: str = ""
    members: tuple[str, ...] = ()
    pattern: re.Pattern[str] | None = None

    @classmethod
    def parse(cls, key: str, token: str) -> Filter:
        """Build a filter for *key* from one value *token*.

        Raises:
            SelectorSyntaxError: If the token is an unusable regex or set.
        """
        if not key:
            raise SelectorSyntaxError("selector filter key must not be empty")
        if token.startswith("\\"):
            return cls(key=key, kind=FilterKind.EXACT, literal=token[1:])
        if token.startswith("~"):
            try:
                pattern = re.compile(token[1:])
            except re.error as exc:
                raise SelectorSyntaxError(f"invalid regex for key {key!r}: {token[1:]!r} ({exc})") from exc
            return cls(key=key, kind=FilterKind.REGEX, pattern=pattern)
        if token.startswith("{") and token.endswith("}"):
            members = tuple(sorted({p.strip() for p in token[1:-1].split(",") if p.strip()}))
            if not members:
                raise SelectorSyntaxError(f"empty set for key {key!r}")
            return cls(key=key, kind=FilterKind.SET, members=members)
        return cls(key=key, kind=FilterKind.EXACT, literal=token)

    def matches(self, values: Sequence[str]) -> bool:
        """True when any of *values* satisfies this term."""
        if self.kind is FilterKind.EXACT:
            return self.literal in values
        if self.kind is FilterKind.SET:
            return any(v in self.members for v in values)
        pattern = self.pattern
        return pattern is not None and any(pattern.search(v) for v in values)


@dataclass(frozen=True, slots=True)
class AliasTable:
    """Short selector names expanding to filter pairs, declared as data.

    Attributes:
        aliases: Mapping of alias name to the tokens it expands to.
    """

    aliases: Mapping[str, tuple[str, ...]]

    def __post_init__(self) -> None:
        object.__setattr__(self, "aliases", _freeze(self.aliases))

    @classmethod
    def empty(cls) -> AliasTable:
        """An alias table declaring nothing."""
        return cls(aliases={})

    @classmethod
    def from_directory(cls, directory: Path) -> AliasTable:
        """Load every ``*.json`` alias file in *directory*, in name order.

        Each file declares ``{"aliases": {"<name>": ["key", "value", ...]}}``.
        An operator extends the vocabulary by adding a file; no code changes.

        Raises:
            SelectorError: If *directory* does not exist.
            SelectorSyntaxError: If a file is malformed or redeclares an alias.
        """
        if not directory.is_dir():
            raise SelectorError(f"alias directory not found: {directory}")
        collected: dict[str, tuple[str, ...]] = {}
        declared_in: dict[str, str] = {}
        for path in sorted(directory.glob("*.json")):
            try:
                loaded: object = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                raise SelectorSyntaxError(f"alias file {path.name} is not valid JSON: {exc}") from exc
            if not isinstance(loaded, dict):
                raise SelectorSyntaxError(f"alias file {path.name} must declare an 'aliases' object")
            declared: object = cast("dict[str, Any]", loaded).get("aliases")
            if not isinstance(declared, dict):
                raise SelectorSyntaxError(f"alias file {path.name} must declare an 'aliases' object")
            for raw_name, raw_tokens in cast("dict[str, Any]", declared).items():
                name = str(raw_name)
                if not isinstance(raw_tokens, list) or not all(
                    isinstance(t, str) for t in cast("list[Any]", raw_tokens)
                ):
                    raise SelectorSyntaxError(f"alias {name!r} in {path.name} must expand to a list of tokens")
                if name in collected:
                    raise SelectorSyntaxError(f"alias {name!r} declared twice: {declared_in[name]} and {path.name}")
                collected[name] = tuple(str(t) for t in cast("list[Any]", raw_tokens))
                declared_in[name] = path.name
        return cls(aliases=collected)


@dataclass(frozen=True, slots=True)
class Selector:
    """A chain of filters resolved against an :class:`InventoryStore`.

    Attributes:
        filters: The terms, in the order they were written.
    """

    filters: tuple[Filter, ...]

    @classmethod
    def parse(cls, tokens: Sequence[str], *, aliases: AliasTable | None = None) -> Selector:
        """Build a selector from *tokens*, expanding any ``@alias`` first.

        Args:
            tokens: Argv-style tokens. A single string is rejected: it is also a
                sequence of its characters, and accepting it would reintroduce
                the shell-string path this API exists to remove.
            aliases: Alias declarations available to the expansion.

        Raises:
            TypeError: If *tokens* is a string.
            SelectorSyntaxError: On an unknown alias, an alias cycle, an odd
                token count or an unusable filter value.
        """
        if isinstance(tokens, str | bytes):
            raise TypeError("Selector.parse takes a sequence of tokens, not a string; split at the source instead")
        expanded = _expand_aliases(tokens, aliases, ())
        if len(expanded) % 2 != 0:
            raise SelectorSyntaxError(f"selector needs an even number of tokens, got {len(expanded)}: {expanded}")
        terms = tuple(Filter.parse(expanded[i], expanded[i + 1]) for i in range(0, len(expanded), 2))
        return cls(filters=terms)

    def matches(self, node: ResolvedNode) -> bool:
        """True when *node* satisfies every term."""
        return all(term.matches(node.values(term.key)) for term in self.filters)


def _expand_aliases(
    tokens: Iterable[str],
    aliases: AliasTable | None,
    stack: tuple[str, ...],
) -> list[str]:
    """Replace every ``@name`` token with the tokens declared for it."""
    out: list[str] = []
    for token in tokens:
        if not token.startswith("@"):
            out.append(token)
            continue
        name = token[1:]
        if aliases is None or name not in aliases.aliases:
            raise SelectorSyntaxError(f"unknown alias: {name!r}")
        if name in stack:
            raise SelectorSyntaxError(f"alias cycle: {' -> '.join([*stack, name])}")
        out.extend(_expand_aliases(aliases.aliases[name], aliases, (*stack, name)))
    return out


@dataclass(frozen=True, slots=True)
class Projection:
    """A ``%field %field`` template rendered once per matched node.

    Attributes:
        template: The template as written.
        fields: The referenced field names, in declaration order.
    """

    template: str
    fields: tuple[str, ...]

    @classmethod
    def parse(cls, template: str) -> Projection:
        """Build a projection from *template*.

        Raises:
            SelectorSyntaxError: If the template references no field.
        """
        names = _FIELD_RE.findall(template)
        if not names:
            raise SelectorSyntaxError(f"projection template references no %field: {template!r}")
        ordered: list[str] = []
        for name in names:
            if name not in ordered:
                ordered.append(name)
        return cls(template=template, fields=tuple(ordered))

    def render(self, node: ResolvedNode) -> str:
        """Render one line for *node*; an unset field renders empty."""
        return _FIELD_RE.sub(lambda m: _render_value(node, m.group(1)), self.template)

    def project(self, node: ResolvedNode) -> dict[str, str]:
        """Return only the projected fields of *node*, for JSON output."""
        return {name: _render_value(node, name) for name in self.fields}


def _render_value(node: ResolvedNode, key: str) -> str:
    """Render the effective values of *key* as one comma-joined string."""
    return ",".join(node.values(key))


def resolve_targets(store: InventoryStore, selector: Selector) -> tuple[ResolvedNode, ...]:
    """Resolve *selector* against *store*, ordered by node identifier.

    Args:
        store: The inventory to query.
        selector: The parsed filter chain.

    Returns:
        The matching nodes with group inheritance applied, sorted by
        ``node_id`` so the same selector over the same store is byte-identical
        across runs and across stores that serialized their rows differently.

    Raises:
        SelectorError: If a node is an edge to a group the store does not declare.
        GroupConflictError: If two groups disagree on a key the node does not set.
    """
    resolved = [_resolve_node(store, node) for node in store.nodes]
    matched = [node for node in resolved if selector.matches(node)]
    return tuple(sorted(matched, key=lambda n: n.node_id))


def _resolve_node(store: InventoryStore, node: InventoryNode) -> ResolvedNode:
    """Apply group inheritance and the reserved keys to one node."""
    effective: dict[str, tuple[str, ...]] = dict(node.attributes)
    inherited: dict[str, list[tuple[str, tuple[str, ...]]]] = {}

    for group_id in sorted(set(node.groups)):
        group = store.group(group_id)
        if group is None:
            raise SelectorError(f"node {node.node_id!r} is a member of undeclared group {group_id!r}")
        for key, values in group.values.items():
            if key in node.attributes or key in RESERVED_KEYS:
                # A value declared on the node itself wins outright, so groups
                # that disagree about it are not a conflict.
                continue
            inherited.setdefault(key, []).append((group_id, values))

    for key in sorted(inherited):
        contributions = inherited[key]
        distinct = {values for _, values in contributions}
        if len(distinct) > 1:
            detail = ", ".join(f"{gid}={'/'.join(values)}" for gid, values in contributions)
            raise GroupConflictError(
                f"node {node.node_id!r} inherits conflicting values for {key!r} ({detail}); "
                f"declare {key!r} on the node to settle it"
            )
        effective[key] = contributions[0][1]

    effective["id"] = (node.node_id,)
    effective["group"] = tuple(sorted(set(node.groups)))
    return ResolvedNode(node_id=node.node_id, attributes=_freeze(effective))


__all__ = [
    "RESERVED_KEYS",
    "AliasTable",
    "Filter",
    "FilterKind",
    "GroupConflictError",
    "InventoryGroup",
    "InventoryNode",
    "InventoryStore",
    "Projection",
    "ResolvedNode",
    "Selector",
    "SelectorError",
    "SelectorSyntaxError",
    "resolve_targets",
]
