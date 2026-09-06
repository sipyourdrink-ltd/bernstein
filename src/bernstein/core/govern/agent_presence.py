"""Whether the governance agent is present on a target, as a probe result (issue #5118).

Nothing answered "which of the targets we know about do not have our agent" --
not because the answer is hard, but because there was no probe recording
presence and no attribute for a selector to ask through.

Two decisions shape this module.

**The gap list is a query, not a report.** A hand-maintained tracking sheet goes
stale; a selector run against the live inventory cannot. So presence is written
onto the node as ordinary attributes and asked for with the ordinary grammar --
``MISSING_AGENT_SELECTOR`` is a selector like any other, and an operator can
write their own.

**`unenrollable` is a PROBE-OBSERVED fact here, not an operator exception.** The
issue leaves that open; this is the choice, and the reason is that the two
failure modes it has to keep apart -- "not yet enrolled" and "cannot ever be
enrolled here" -- are only distinguishable if something looked. An operator
exclusion is a different thing (a decision, revocable, needing an author and a
date), and folding it in here would make one attribute mean both "we tried and
it refused" and "we chose not to try", which is the collapse this exists to
prevent. Declared exclusions are a follow-up, and they belong on their own key.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

from bernstein.core.govern.selector import InventoryNode, Selector

if TYPE_CHECKING:
    from collections.abc import Mapping

    from bernstein.core.govern.selector import InventoryStore, ResolvedNode

#: Attribute keys the probe writes. Named constants because a selector matches
#: on the literal string, and a typo in either place is a query that silently
#: matches nothing.
AGENT_PRESENT_KEY = "agent_present"
AGENT_VERSION_KEY = "agent_version"
ENROLLMENT_KEY = "enrollment"
ENROLLMENT_REASON_KEY = "enrollment_reason"

#: Attribute values for `agent_present`. Strings, because every selector
#: attribute is multi-valued strings -- a bool here would be a second encoding
#: of the same fact that the grammar cannot match on.
PRESENT_TRUE = "true"
PRESENT_FALSE = "false"


class Enrollment(StrEnum):
    """What the probe learned about this target's enrollment.

    Deliberately three states. ``UNENROLLABLE`` is not "absent, but worse": it
    is a different answer, and collapsing it into ``MISSING`` is what makes a
    gap list unactionable -- somebody works through it and re-tries targets that
    already refused.
    """

    #: The agent answered.
    ENROLLED = "enrolled"
    #: The agent is not there, and nothing said it could not be.
    MISSING = "missing"
    #: The probe reached the target and learned it cannot host the agent.
    UNENROLLABLE = "unenrollable"


@dataclass(frozen=True, slots=True)
class AgentPresence:
    """One probe's result for one target.

    Attributes:
        node_id: The target probed.
        present: Whether the agent answered.
        version: The version it reported, or ``""``. Empty when absent, and
            empty is also the honest answer for an agent that answered without
            naming a version -- ``unknown`` would be a claim the probe cannot
            make.
        enrollment: :class:`Enrollment`.
        reason: Why, when the target is ``UNENROLLABLE``. Recorded rather than
            merely excluding the target, so "cannot ever be enrolled here" does
            not read as "not yet".
    """

    node_id: str
    present: bool
    version: str = ""
    enrollment: Enrollment = Enrollment.MISSING
    reason: str = ""

    @classmethod
    def enrolled(cls, node_id: str, version: str = "") -> AgentPresence:
        """The agent answered on this target."""
        return cls(node_id=node_id, present=True, version=version, enrollment=Enrollment.ENROLLED)

    @classmethod
    def missing(cls, node_id: str) -> AgentPresence:
        """The agent is not there, and nothing said it could not be."""
        return cls(node_id=node_id, present=False, enrollment=Enrollment.MISSING)

    @classmethod
    def unenrollable(cls, node_id: str, reason: str) -> AgentPresence:
        """The probe reached the target and learned it cannot host the agent.

        Raises:
            ValueError: *reason* is empty. An unenrollable target with no reason
                is indistinguishable from one nobody has got to yet, which is
                the distinction this state exists to keep.
        """
        if not reason.strip():
            raise ValueError("an unenrollable target must record why; without it the state says nothing")
        return cls(node_id=node_id, present=False, enrollment=Enrollment.UNENROLLABLE, reason=reason.strip())

    def attributes(self) -> Mapping[str, tuple[str, ...]]:
        """The probe's result as selector attributes.

        The reason is omitted when empty rather than written as ``""``: an
        attribute present with an empty value would match ``enrollment_reason``
        queries and report a reason nobody gave.
        """
        result: dict[str, tuple[str, ...]] = {
            AGENT_PRESENT_KEY: (PRESENT_TRUE if self.present else PRESENT_FALSE,),
            ENROLLMENT_KEY: (self.enrollment.value,),
        }
        if self.version:
            result[AGENT_VERSION_KEY] = (self.version,)
        if self.reason:
            result[ENROLLMENT_REASON_KEY] = (self.reason,)
        return result


def apply_presence(node: InventoryNode, presence: AgentPresence) -> InventoryNode:
    """Return *node* carrying this probe's result.

    The probe's keys REPLACE any it already had, and every other attribute is
    kept. A probe result is the current reading; merging it with an older one
    would leave a node claiming two versions at once.
    """
    merged = {key: values for key, values in node.attributes.items() if key not in _PROBE_KEYS}
    merged.update(presence.attributes())
    return InventoryNode(node_id=node.node_id, attributes=merged, groups=node.groups)


_PROBE_KEYS = frozenset({AGENT_PRESENT_KEY, AGENT_VERSION_KEY, ENROLLMENT_KEY, ENROLLMENT_REASON_KEY})

#: Targets that do not have the agent, for whichever reason -- the gap list.
#:
#: Both non-enrolled states in one set, because the question "which targets lack
#: it" is one question. Which KIND of gap each is stays readable on the node, so
#: a caller narrows with `MISSING_AGENT_SELECTOR` or `UNENROLLABLE_SELECTOR`
#: rather than by post-filtering a list this returned.
NO_AGENT_SELECTOR: Selector = Selector.parse(
    [ENROLLMENT_KEY, f"{{{Enrollment.MISSING.value},{Enrollment.UNENROLLABLE.value}}}"]
)

#: Targets that could be enrolled and are not. The actionable half of the gap.
MISSING_AGENT_SELECTOR: Selector = Selector.parse([ENROLLMENT_KEY, Enrollment.MISSING.value])

#: Targets the probe found cannot host the agent. Not work, but not invisible.
UNENROLLABLE_SELECTOR: Selector = Selector.parse([ENROLLMENT_KEY, Enrollment.UNENROLLABLE.value])


def enrollment_gap(store: InventoryStore) -> tuple[ResolvedNode, ...]:
    """Every target that does not have the agent, ordered by node identifier.

    A thin call over :func:`~bernstein.core.govern.selector.resolve_targets` with
    :data:`NO_AGENT_SELECTOR`, so the gap list is the same query an operator can
    write by hand -- and stays live rather than going stale the way a maintained
    sheet does.

    A node the probe never ran against carries no ``enrollment`` attribute and
    therefore does NOT appear. That is deliberate: "we have not looked" is not
    "the agent is missing", and reporting it as a gap would put unprobed targets
    on a list of work nobody can do until a probe runs.
    """
    from bernstein.core.govern.selector import resolve_targets

    return resolve_targets(store, NO_AGENT_SELECTOR)


__all__ = [
    "AGENT_PRESENT_KEY",
    "AGENT_VERSION_KEY",
    "ENROLLMENT_KEY",
    "ENROLLMENT_REASON_KEY",
    "MISSING_AGENT_SELECTOR",
    "NO_AGENT_SELECTOR",
    "PRESENT_FALSE",
    "PRESENT_TRUE",
    "UNENROLLABLE_SELECTOR",
    "AgentPresence",
    "Enrollment",
    "apply_presence",
    "enrollment_gap",
]
