"""The typed action vocabulary and deterministic action rendering (#2548).

Rules no longer only create tasks. A rule dispatches a typed action - notify,
pause or resume a schedule, cancel or suspend a task, clamp a budget envelope,
drain the warm pool, pin an adapter to its last green version, or force a
checkpoint-fork retry. This module is the pure half: the action grammar, the
rule hash, and the deterministic renderer that binds an action to its triggering
event. The receipt gate and the chain-writing dispatch live in
:mod:`bernstein.core.events.receipts`.

Determinism is the whole point: the same rule against the same event bytes
renders a byte-identical action, so a fire receipt can commit to the rendered
action's digest before the effect ever runs.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Mapping

    from bernstein.core.events.grammar import CanonicalEvent

#: The closed set of action kinds a rule may dispatch. Each maps to an existing
#: subsystem: the notifications bridge, the schedule store, the task lifecycle,
#: budget actions, the warm pool, the adapter last-green pin, and checkpoint
#: retry. The set is closed so an operator config cannot smuggle an arbitrary
#: effect through the renderer.
ALLOWED_ACTION_KINDS: frozenset[str] = frozenset(
    {
        "notify",
        "schedule.pause",
        "schedule.resume",
        "task.cancel",
        "task.suspend",
        "budget.clamp",
        "warm_pool.drain",
        "adapter.pin_last_green",
        "retry.checkpoint_fork",
    }
)


def _canonical_json(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def rule_hash(rule_spec: Mapping[str, Any]) -> str:
    """Return the ``sha256:`` identity of a rule specification.

    The pre-image is the canonical JSON of the operator's full rule mapping, so
    two byte-identical rules hash identically and any edit changes the hash. The
    rule hash is what a fire receipt commits to.
    """
    return "sha256:" + hashlib.sha256(_canonical_json(dict(rule_spec)).encode("utf-8")).hexdigest()


class UnknownActionKind(ValueError):
    """Raised when an action spec names a kind outside the closed vocabulary."""


@dataclass(frozen=True, slots=True)
class ActionSpec:
    """A typed action a rule may dispatch, before rendering.

    Attributes:
        kind: One of :data:`ALLOWED_ACTION_KINDS`.
        params: Static parameters. String values may contain ``{event.<field>}``
            tokens that the renderer substitutes from the triggering event.
    """

    kind: str
    params: dict[str, Any]

    def __post_init__(self) -> None:
        if self.kind not in ALLOWED_ACTION_KINDS:
            raise UnknownActionKind(f"unknown action kind: {self.kind!r}; allowed: {sorted(ALLOWED_ACTION_KINDS)}")


@dataclass(frozen=True, slots=True)
class RenderedAction:
    """An action bound to its triggering event, ready to execute.

    Attributes:
        kind: The action kind (copied from the spec).
        params: The rendered parameters, tokens substituted.
        triggering_event_hmac: The HMAC of the event that rendered this action -
            the binding a fire receipt asserts.
    """

    kind: str
    params: dict[str, Any]
    triggering_event_hmac: str

    @property
    def digest(self) -> str:
        """Return the ``sha256:`` digest of the rendered action.

        Covers kind, params, and the triggering event HMAC, so an action's
        digest is inseparable from the event that produced it.
        """
        preimage = _canonical_json(
            {
                "kind": self.kind,
                "params": self.params,
                "triggering_event_hmac": self.triggering_event_hmac,
            }
        )
        return "sha256:" + hashlib.sha256(preimage.encode("utf-8")).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        """Return the canonical serialisable mapping for this rendered action."""
        return {
            "kind": self.kind,
            "params": self.params,
            "triggering_event_hmac": self.triggering_event_hmac,
        }


def _render_value(value: Any, tokens: dict[str, str]) -> Any:
    """Substitute ``{event.<field>}`` tokens inside a string value.

    Non-string values pass through unchanged. Substitution is exact-token
    replacement (no format-spec parsing), so a params value like
    ``"{event.resource_id}"`` becomes the event's resource id verbatim.
    """
    if not isinstance(value, str):
        return value
    rendered = value
    for token, replacement in tokens.items():
        rendered = rendered.replace(token, replacement)
    return rendered


def render_action(spec: ActionSpec, triggering_event: CanonicalEvent) -> RenderedAction:
    """Render ``spec`` against ``triggering_event`` deterministically.

    The same spec against the same event bytes always renders the same action,
    which is what makes the fire receipt's commitment meaningful.
    """
    tokens = {
        "{event.hmac}": triggering_event.hmac,
        "{event.resource_id}": triggering_event.resource_id,
        "{event.label}": triggering_event.label,
        "{event.actor}": triggering_event.actor,
        "{event.payload_digest}": triggering_event.payload_digest,
    }
    rendered_params = {key: _render_value(val, tokens) for key, val in sorted(spec.params.items())}
    return RenderedAction(
        kind=spec.kind,
        params=rendered_params,
        triggering_event_hmac=triggering_event.hmac,
    )


__all__ = [
    "ALLOWED_ACTION_KINDS",
    "ActionSpec",
    "RenderedAction",
    "UnknownActionKind",
    "render_action",
    "rule_hash",
]
