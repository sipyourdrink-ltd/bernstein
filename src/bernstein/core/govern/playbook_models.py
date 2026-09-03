"""Playbook models for the govern plan subsystem.

The playbook represents declared posture: a set of clauses describing what
is permitted, required, or forbidden in the environment. It is the "desired
state" against which the inventory is diffed to produce a GovernPlan.

A clause may also declare *how* to close the gap it judges, as an ordered
change set of :class:`RemediationAction` records. The remedy is data, not a
script: it is part of the clause and therefore part of the playbook's content
address, so a posture whose declared remedy was swapped is a different posture.
The field is optional, and a clause that declares none is reported as such
rather than treated as already remedied.

The playbook is data, not a script: parsing it either produces a fully-typed
object or raises. A field the schema does not know about is rejected, never
silently dropped, and a clause naming a principal class the playbook did not
declare is rejected too -- a ceiling nothing can satisfy or violate is not a
looser posture, it is a typo. Two playbooks that declare the same rules in a
different order are the same declared posture, so :meth:`Playbook.content_hash`
canonicalizes clause order before hashing: the digest names the *content*,
not the author's typing order.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, cast


@dataclass(frozen=True, slots=True)
class RemediationAction:
    """One declared change in a clause's remediation plan.

    The action is the executable unit an operator applies to close the gap the
    clause judges. It states the change, never performs it.

    Attributes:
        action: The verb to apply, e.g. ``set``, ``remove``, ``add``.
        target: What the verb applies to (a path, an ARN, a config key).
        value: The value the verb writes. None for verbs that take none.
    """

    action: str
    target: str
    value: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Return the canonical serialization, omitting an absent value."""
        result: dict[str, Any] = {"action": self.action, "target": self.target}
        if self.value is not None:
            result["value"] = self.value
        return result

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> RemediationAction:
        """Rebuild an action from a serialized dict.

        Raises:
            ValueError: When the record omits ``action`` or ``target``, leaves
                either empty, or carries an unknown key. An unparseable remedy
                is a failed remedy, never an empty one.
        """
        unknown = set(raw) - {"action", "target", "value"}
        if unknown:
            raise ValueError(f"remediation_plan step has unknown key(s): {sorted(unknown)}")
        for field_name in ("action", "target"):
            if not str(raw.get(field_name, "")).strip():
                raise ValueError(f"remediation_plan step is missing required field {field_name!r}")
        value = raw.get("value")
        return cls(
            action=str(raw["action"]),
            target=str(raw["target"]),
            value=None if value is None else str(value),
        )


#: The three kinds a clause may declare. Anything else is a typo, not a
#: looser schema -- rejected at construction, not carried through as an
#: opaque string.
_KNOWN_KINDS = frozenset({"forbidden", "required", "permitted"})

#: Keys a clause dict may carry. Declared once so ``from_dict`` can tell a
#: key the schema does not know about from one it merely left unset.
_CLAUSE_KEYS = frozenset(
    {
        "surface",
        "clause",
        "kind",
        "declared_value",
        "declared_ceiling",
        "principal_class",
        "remediation_plan",
    }
)

#: Keys a playbook dict may carry, for the same reason.
_PLAYBOOK_KEYS = frozenset({"clauses", "principal_classes"})


class PlaybookValidationError(ValueError):
    """Raised when a playbook or clause fails validation.

    Covers three distinct failures, all reported as this one error so a
    caller can catch "this playbook is not usable" without enumerating
    variants: an unrecognized field, a clause ``kind`` outside the declared
    set, and a clause whose ``principal_class`` was never declared on the
    enclosing playbook.
    """


@dataclass(frozen=True, slots=True)
class PlaybookClause:
    """A single declared posture clause.

    Attributes:
        surface: The resource or surface this clause governs.
        clause: Human-readable description of the posture requirement.
            e.g., "No public S3 buckets" or "IAM policies must have MFA".
        kind: Classification of this clause: ``forbidden``, ``required``,
            or ``permitted``.
        declared_value: For ``required`` clauses: the value that must be
            present. For ``permitted`` clauses with ceilings: the maximum
            allowed value. None for ``forbidden`` clauses without values.
        declared_ceiling: For ``permitted`` clauses: the maximum allowed
            value. None if not applicable.
        remediation_plan: The ordered change set that closes a gap this clause
            judges. None when the clause declares no remedy — an absence that
            is reported, not silently read as "nothing to do".
        principal_class: The class of principal (e.g. ``worker``,
            ``manager``) this clause's ceiling applies to. None for clauses
            that are not scoped to a principal class. When set, it must name
            a class the enclosing :class:`Playbook` declares in
            ``principal_classes`` -- validated in :meth:`Playbook.__post_init__`
            because a bare clause has no view of its siblings.
    """

    surface: str
    clause: str
    kind: str  # "forbidden" | "required" | "permitted"
    declared_value: str | None = None
    declared_ceiling: str | None = None
    remediation_plan: tuple[RemediationAction, ...] | None = None
    principal_class: str | None = None

    def __post_init__(self) -> None:
        if self.kind not in _KNOWN_KINDS:
            raise PlaybookValidationError(
                f"unknown clause kind {self.kind!r} for surface {self.surface!r}: must be one of {sorted(_KNOWN_KINDS)}"
            )

    def to_dict(self) -> dict[str, Any]:
        """Return the canonical serialization."""
        result: dict[str, Any] = {
            "surface": self.surface,
            "clause": self.clause,
            "kind": self.kind,
        }
        if self.declared_value is not None:
            result["declared_value"] = self.declared_value
        if self.declared_ceiling is not None:
            result["declared_ceiling"] = self.declared_ceiling
        if self.remediation_plan is not None:
            result["remediation_plan"] = [a.to_dict() for a in self.remediation_plan]
        if self.principal_class is not None:
            result["principal_class"] = self.principal_class
        return result

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> PlaybookClause:
        """Rebuild a clause from a serialized dict.

        Raises:
            PlaybookValidationError: *raw* carries a key this schema does not
                know, or ``kind`` is outside the declared set.
        """
        unknown = set(raw) - _CLAUSE_KEYS
        if unknown:
            raise PlaybookValidationError(
                f"unknown clause field(s) {sorted(unknown)} for surface "
                f"{raw.get('surface')!r}: known fields are {sorted(_CLAUSE_KEYS)}"
            )
        return cls(
            surface=str(raw["surface"]),
            clause=str(raw["clause"]),
            kind=str(raw["kind"]),
            declared_value=raw.get("declared_value"),
            declared_ceiling=raw.get("declared_ceiling"),
            remediation_plan=parse_remediation_plan(raw.get("remediation_plan")),
            principal_class=raw.get("principal_class"),
        )


@dataclass(frozen=True, slots=True)
class Playbook:
    """A declared posture specification.

    The playbook is a tuple of clauses, each declaring a posture rule for a
    specific surface, plus the set of principal classes a clause may scope
    a ceiling to. Tuple ensures immutability; hashing canonicalizes order
    (see the module docstring) rather than relying on it.

    Attributes:
        clauses: Tuple of declared posture clauses.
        principal_classes: The principal classes this playbook declares.
            A clause naming a ``principal_class`` outside this set fails to
            construct -- see :meth:`__post_init__`.
    """

    clauses: tuple[PlaybookClause, ...]
    principal_classes: tuple[str, ...] = field(default=())

    def __post_init__(self) -> None:
        declared = frozenset(self.principal_classes)
        for c in self.clauses:
            if c.principal_class is not None and c.principal_class not in declared:
                raise PlaybookValidationError(
                    f"clause for surface {c.surface!r} references undeclared "
                    f"principal class {c.principal_class!r}: declared classes "
                    f"are {sorted(declared)}"
                )

    def to_dict(self) -> dict[str, Any]:
        """Return the canonical serialization."""
        result: dict[str, Any] = {"clauses": [c.to_dict() for c in self.clauses]}
        if self.principal_classes:
            result["principal_classes"] = list(self.principal_classes)
        return result

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Playbook:
        """Rebuild a playbook from a serialized dict.

        Raises:
            PlaybookValidationError: *raw* (or a nested clause) carries a key
                this schema does not know, a clause ``kind`` is outside the
                declared set, or a clause references an undeclared principal
                class.
        """
        unknown = set(raw) - _PLAYBOOK_KEYS
        if unknown:
            raise PlaybookValidationError(
                f"unknown playbook field(s) {sorted(unknown)}: known fields are {sorted(_PLAYBOOK_KEYS)}"
            )
        clauses = tuple(PlaybookClause.from_dict(c) for c in raw.get("clauses", []))
        principal_classes = tuple(str(p) for p in raw.get("principal_classes", ()))
        return cls(clauses=clauses, principal_classes=principal_classes)

    def content_hash(self) -> str:
        """Compute a stable content hash of the playbook.

        Uses canonical JSON (sorted keys, minimal separators, UTF-8) over a
        *canonicalized* structure -- clauses sorted by their own canonical
        JSON and principal classes sorted -- so two playbooks that declare
        the same posture in a different order (a different author, a
        different tool, a reformatted file) produce the same digest. Only a
        genuine difference in declared clauses or principal classes changes
        it.
        """
        canonical_clauses = sorted(
            (c.to_dict() for c in self.clauses),
            key=lambda c: json.dumps(c, ensure_ascii=False, sort_keys=True),
        )
        canonical = json.dumps(
            {
                "clauses": canonical_clauses,
                "principal_classes": sorted(self.principal_classes),
            },
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return "sha256:" + hashlib.sha256(canonical).hexdigest()

    def clauses_by_kind(self, kind: str) -> tuple[PlaybookClause, ...]:
        """Return all clauses matching the given kind."""
        return tuple(c for c in self.clauses if c.kind == kind)

    def surface_ids(self) -> frozenset[str]:
        """Return the set of all surface identifiers in this playbook."""
        return frozenset(c.surface for c in self.clauses)


def parse_remediation_plan(raw: Any) -> tuple[RemediationAction, ...] | None:
    """Parse a clause's ``remediation_plan`` field.

    Returns None when the field is absent, so callers can tell "no remedy was
    declared" from "an empty remedy was declared".

    Raises:
        ValueError: When the field is present but is not a list of well-formed
            action records.
    """
    if raw is None:
        return None
    if not isinstance(raw, list):
        raise ValueError(f"remediation_plan must be a list, got {type(raw).__name__}")
    actions: list[RemediationAction] = []
    for step in cast("list[Any]", raw):
        if not isinstance(step, dict):
            raise ValueError(f"remediation_plan step must be an object, got {type(step).__name__}")
        actions.append(RemediationAction.from_dict(cast("dict[str, Any]", step)))
    return tuple(actions)


__all__ = [
    "Playbook",
    "PlaybookClause",
    "PlaybookValidationError",
    "RemediationAction",
    "parse_remediation_plan",
]
