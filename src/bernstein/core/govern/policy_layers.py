"""Layered policy composition, in a fixed order, explainable per target (issue #5117).

`PlaybookClause` declares posture as a flat list: every clause is `forbidden`,
`required` or `permitted`, with no notion of layering and no record of where a
clause came from. An operator staring at one target's effective policy needs to
know not just what applies but WHY -- which layer wrote which clause.

The order is fixed and it is the whole point::

    classification -> baseline -> instrumentation -> exactly one class overlay

Later layers win, so a class overlay can tighten or relax what the baseline
declared, and nothing else can silently reorder that. Composition being data
rather than a precedence rule buried in a function means "why is this the
effective value" is answerable by reading the result rather than the source.

Three decisions worth naming.

**Zero or more than one class overlay is a FINDING, not a default.** `kind` has
no way to express "ambiguous", so a target matching two overlays would otherwise
take whichever the iteration order reached last -- an answer that depends on file
layout and changes when somebody adds an unrelated overlay above it.

**The baseline's ORDER is part of the hash.** A silent reorder -- same
sub-policies, different precedence -- would otherwise be the one kind of edit a
desired-state diff cannot see, precisely because it changes what wins without
changing what is declared.

**A layer's identity in the hash is its name and position, not its object.** Two
runs that load the same document must hash the same, and two documents that
declare the same layers in a different order must not.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from bernstein.core.govern.playbook_models import PlaybookClause


class LayerKind(StrEnum):
    """The four layers, in composition order.

    Declaration order here IS the composition order, and
    :data:`COMPOSITION_ORDER` derives from it rather than repeating it -- two
    lists of the same order is one list plus a way to disagree with it.
    """

    #: Facts about what the target IS. First, because everything above may key on them.
    CLASSIFICATION = "classification"
    #: The common posture every target carries.
    BASELINE = "baseline"
    #: Telemetry and audit hooks, above the baseline so a fleet-wide observability
    #: requirement is not something an individual baseline entry can drop.
    INSTRUMENTATION = "instrumentation"
    #: Exactly one per target. Last, so a class can tighten or relax the rest.
    CLASS_OVERLAY = "class_overlay"


#: The fixed order, derived from the enum so the two cannot drift apart.
COMPOSITION_ORDER: tuple[LayerKind, ...] = tuple(LayerKind)


class PolicyCompositionError(ValueError):
    """Raised when a layer set cannot be composed as declared."""


@dataclass(frozen=True, slots=True)
class PolicyLayer:
    """One named layer of declared posture.

    Attributes:
        kind: Which of the four layers this is.
        name: The layer's name, carried onto every clause it contributes so an
            explain names the source rather than only the tier.
        clauses: The clauses it declares.
        applies_to: For a ``class_overlay``, the classification values it covers.
            Empty on every other kind -- a baseline applies to everything by
            definition, and letting it carry a selector would make "the common
            baseline" a thing that silently does not apply somewhere.
    """

    kind: LayerKind
    name: str
    clauses: tuple[PlaybookClause, ...] = ()
    applies_to: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise PolicyCompositionError(f"a {self.kind.value} layer must be named")
        if self.applies_to and LayerKind.CLASS_OVERLAY is not self.kind:
            raise PolicyCompositionError(
                f"layer {self.name!r} is a {self.kind.value} and cannot declare `applies_to`; "
                "only a class overlay is scoped to a class"
            )
        if LayerKind.CLASS_OVERLAY is self.kind and not self.applies_to:
            raise PolicyCompositionError(
                f"class overlay {self.name!r} declares no `applies_to`, so it can never be selected"
            )

    def to_dict(self) -> dict[str, Any]:
        """Return the canonical serialization."""
        result: dict[str, Any] = {
            "kind": self.kind.value,
            "name": self.name,
            "clauses": [clause.to_dict() for clause in self.clauses],
        }
        if self.applies_to:
            result["applies_to"] = list(self.applies_to)
        return result


@dataclass(frozen=True, slots=True)
class EffectiveClause:
    """One clause that survived composition, and where it came from.

    Attributes:
        clause: The clause itself.
        layer: Which tier wrote it.
        source: The layer's name -- the answer to "which baseline entry", which
            the tier alone does not give.
        overridden: The layers that declared this same key earlier and lost, in
            composition order. Empty when nothing was overridden. Kept because
            "this came from the overlay" and "this came from the overlay,
            overriding the baseline" are different answers to "why".
    """

    clause: PlaybookClause
    layer: LayerKind
    source: str
    overridden: tuple[str, ...] = ()

    @property
    def key(self) -> tuple[str, str]:
        """What identifies this clause across layers: the surface and the clause text."""
        return (self.clause.surface, self.clause.clause)

    def to_dict(self) -> dict[str, Any]:
        """Return the canonical serialization, for an explain's ``--json``."""
        return {
            "layer": self.layer.value,
            "source": self.source,
            "overridden": list(self.overridden),
            **self.clause.to_dict(),
        }


@dataclass(frozen=True, slots=True)
class OverlayFinding:
    """A target whose class overlay could not be chosen.

    Attributes:
        target: The target.
        matched: The overlays that matched, in declared order. Empty means none
            did.
        reason: Why this is a finding, in one line.
    """

    target: str
    matched: tuple[str, ...]
    reason: str

    def to_dict(self) -> dict[str, Any]:
        """Return the canonical serialization."""
        return {"target": self.target, "matched": list(self.matched), "reason": self.reason}


#: One printable row of an explain: the surface, the clause, and ``layer:source``.
#:
#: Named because the tuple was annotated as a single fixed-length triple where a
#: VARIABLE-length sequence of them was returned -- an annotation that read as
#: correct and described a different shape. A named alias makes the two halves of
#: `explain`'s return type impossible to confuse for one another.
ExplainRow = tuple[str, str, str]


@dataclass(frozen=True, slots=True)
class EffectivePolicy:
    """One target's composed policy, and any finding that composing it raised.

    Attributes:
        target: The target this was composed for.
        clauses: The effective clauses, ordered by surface then clause text, so
            two runs over one document print identically.
        finding: The overlay finding, when the overlay could not be chosen.
            ``None`` on a clean composition.
    """

    target: str
    clauses: tuple[EffectiveClause, ...] = ()
    finding: OverlayFinding | None = None

    @property
    def is_ambiguous(self) -> bool:
        """Whether composing this target raised an overlay finding."""
        return self.finding is not None

    def explain(self) -> tuple[tuple[ExplainRow, ...], str | None]:
        """Every effective clause as a printable row, plus the finding's reason.

        The shape a `--explain` table prints, kept here rather than in the CLI so
        the answer does not depend on which command asked.

        Returns:
            ``(rows, reason)`` -- one :data:`ExplainRow` per effective clause in
            the order :attr:`clauses` holds them, and the overlay finding's
            reason or ``None``.
        """
        rows = tuple(
            (entry.clause.surface, entry.clause.clause, f"{entry.layer.value}:{entry.source}") for entry in self.clauses
        )
        return rows, None if self.finding is None else self.finding.reason

    def to_dict(self) -> dict[str, Any]:
        """Return the canonical serialization."""
        return {
            "target": self.target,
            "clauses": [entry.to_dict() for entry in self.clauses],
            "finding": None if self.finding is None else self.finding.to_dict(),
        }


@dataclass(frozen=True, slots=True)
class PolicySet:
    """Every declared layer, and the composition over them.

    Attributes:
        layers: The layers as declared. ORDER MATTERS within a kind: two
            baseline entries declaring the same key are resolved by declaration
            order, which is why that order is part of the hash.
    """

    layers: tuple[PolicyLayer, ...] = field(default_factory=tuple)

    def of_kind(self, kind: LayerKind) -> tuple[PolicyLayer, ...]:
        """The declared layers of one kind, in declaration order."""
        return tuple(layer for layer in self.layers if layer.kind is kind)

    def content_hash(self) -> str:
        """A stable hash over the layers, their ORDER, and their clauses.

        The position is hashed alongside the name, so reordering the baseline --
        same sub-policies, different precedence -- moves the hash. Without it,
        the one edit that changes what wins without changing what is declared
        would be the one edit a desired-state diff cannot see.
        """
        document = [{"position": index, **layer.to_dict()} for index, layer in enumerate(self.layers)]
        canonical = json.dumps(document, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
        return "sha256:" + hashlib.sha256(canonical).hexdigest()

    def compose(self, target: str, classifications: Iterable[str] = ()) -> EffectivePolicy:
        """Compose this target's effective policy.

        Args:
            target: The target being composed for.
            classifications: The classification values that decide which class
                overlay applies.

        Returns:
            The effective clauses with their source, plus an
            :class:`OverlayFinding` when zero or more than one overlay matched.
            An ambiguous target still composes everything BELOW the overlay: the
            baseline and instrumentation apply whatever the class turns out to
            be, and reporting nothing would hide posture that is not in doubt.
        """
        values = set(classifications)
        matched = tuple(layer.name for layer in self.of_kind(LayerKind.CLASS_OVERLAY) if values & set(layer.applies_to))
        finding = _overlay_finding(target, matched)

        effective: dict[tuple[str, str], EffectiveClause] = {}
        for kind in COMPOSITION_ORDER:
            for layer in self.of_kind(kind):
                if LayerKind.CLASS_OVERLAY is kind and layer.name not in matched:
                    continue
                # An ambiguous overlay contributes nothing: applying one of two
                # candidates would be the silent default this refuses to make.
                if LayerKind.CLASS_OVERLAY is kind and finding is not None:
                    continue
                for clause in layer.clauses:
                    key = (clause.surface, clause.clause)
                    previous = effective.get(key)
                    overridden = (
                        () if previous is None else (*previous.overridden, f"{previous.layer.value}:{previous.source}")
                    )
                    effective[key] = EffectiveClause(
                        clause=clause, layer=kind, source=layer.name, overridden=overridden
                    )
        ordered = tuple(effective[key] for key in sorted(effective))
        return EffectivePolicy(target=target, clauses=ordered, finding=finding)


def _overlay_finding(target: str, matched: Sequence[str]) -> OverlayFinding | None:
    """The finding for a target that did not match exactly one class overlay."""
    if len(matched) == 1:
        return None
    if len(matched) == 0:
        return OverlayFinding(
            target=target,
            matched=(),
            reason="no class overlay applies to this target, so its class-specific posture is undeclared",
        )
    return OverlayFinding(
        target=target,
        matched=tuple(matched),
        reason=(
            f"{len(matched)} class overlays apply to this target ({', '.join(matched)}); "
            "which one wins would depend on declaration order, so neither is applied"
        ),
    )


__all__ = [
    "COMPOSITION_ORDER",
    "EffectiveClause",
    "EffectivePolicy",
    "LayerKind",
    "OverlayFinding",
    "PolicyCompositionError",
    "PolicyLayer",
    "PolicySet",
]
