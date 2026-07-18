"""Per-action lineage primitives for browser / computer-use agents (#2606).

Where :mod:`bernstein.core.agents.multimodal` and
:mod:`bernstein.core.agents.multimodal_attestation` capture an operator-supplied
attachment *into* a run, this module captures the outbound side: the stream of
GUI actions a *third-party autonomous* browser / computer-use agent decides on
against a live UI.

A coding adapter's output is a file diff, which the lineage recorder already
hashes, chains, and signs. A browser / computer-use agent instead owns its own
decision loop and emits a sequence of actions (navigate, type, click). Those
actions are non-deterministic by construction, so absent an anchored log they
are unauditable -- the one property every other agent kind on the substrate does
not have.

The anchor definition (the load-bearing primitive) is::

    observation_hash = sha256(pre_action_screenshot_bytes + dom_accessibility_digest)
    action_anchor    = sha256(canonical(prev_anchor, observation_hash, action))

Each ``action_anchor`` folds in the prior anchor, so the action stream is a
single-parent Merkle chain: the head anchor is the run's identity, and a
non-deterministic re-run that observed different bytes or chose a different
action surfaces as a hash mismatch at the exact action index rather than a
flaky text assertion.

Only *digests* of typed values are ever anchored -- never the raw keystrokes --
so a form-filling agent's secrets never enter the chain in plain text.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import StrEnum

# ---------------------------------------------------------------------------
# Action vocabulary
# ---------------------------------------------------------------------------


class ActionKind(StrEnum):
    """The kind of GUI action an external agent decided on.

    Kept deliberately small and driver-agnostic: the anchor logic never
    imports a specific browser tool, so a concrete driver maps its own verbs
    onto these before recording.
    """

    NAVIGATE = "navigate"
    CLICK = "click"
    TYPE = "type"
    SCROLL = "scroll"
    KEY = "key"
    SELECT = "select"
    SUBMIT = "submit"
    WAIT = "wait"
    SCREENSHOT = "screenshot"


#: Sentinel ``prev_anchor`` for the first action in a run. The genesis action
#: folds this constant in so the chain has a well-known root that a verifier
#: reproduces without any prior state.
GENESIS_ANCHOR = ""


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Action:
    """A single canonicalised action the external agent chose.

    Attributes:
        kind: The action verb (see :class:`ActionKind`).
        target: The action target -- a URL, CSS/accessibility selector, or
            element ref. Free-form but canonicalised into the anchor as-is.
        value_digest: SHA-256 hex digest of any typed value. The raw value is
            never stored or anchored, so secrets typed into a form never enter
            the lineage chain in plain text. Empty for actions that type
            nothing (click, navigate, scroll).
    """

    kind: ActionKind
    target: str = ""
    value_digest: str = ""

    def to_canonical_dict(self) -> dict[str, str]:
        """Return the canonical dict folded into the anchor preimage."""
        return {
            "kind": str(self.kind),
            "target": self.target,
            "value_digest": self.value_digest,
        }


@dataclass(frozen=True, slots=True)
class ActionObservation:
    """The pre-action observation an anchor binds to.

    Attributes:
        screenshot_sha256: CAS key (SHA-256 hex) of the pre-action screenshot
            bytes. The bytes themselves live in the content-addressed store.
        dom_digest: A normalised accessibility/DOM digest (hex). Hashing a
            normalised accessibility tree rather than raw HTML keeps the digest
            stable across cosmetically noisy dynamic pages.
        observation_hash: ``sha256(screenshot_bytes + dom_digest)``. This is the
            value folded into the action anchor; it binds the anchor to the
            exact bytes the agent saw before it acted.
    """

    screenshot_sha256: str
    dom_digest: str
    observation_hash: str


# ---------------------------------------------------------------------------
# Digest + anchor helpers
# ---------------------------------------------------------------------------


def digest_typed_value(value: str | bytes) -> str:
    """Return the SHA-256 hex digest of a typed value.

    Used to anchor *what* an agent typed without ever recording the raw value.
    """
    raw = value.encode("utf-8") if isinstance(value, str) else value
    return hashlib.sha256(raw).hexdigest()


def compute_observation_hash(*, screenshot_bytes: bytes, dom_digest: str) -> str:
    """Return ``sha256(screenshot_bytes + dom_digest)``.

    The two inputs are concatenated in a fixed order (screenshot bytes first,
    then the UTF-8 bytes of the accessibility digest) so the value is stable
    and reproducible from the stored bytes on replay.
    """
    h = hashlib.sha256(screenshot_bytes)
    h.update(dom_digest.encode("utf-8"))
    return h.hexdigest()


def _canonical(body: dict[str, object]) -> bytes:
    """RFC 8785-style canonical JSON bytes (sorted keys, minimal separators)."""
    return json.dumps(
        body,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def action_anchor_preimage(*, prev_anchor: str, observation_hash: str, action: Action) -> bytes:
    """Return the canonical bytes whose SHA-256 is the action anchor.

    These bytes are also what the lineage recorder hashes into the entry's
    ``content_hash``, so ``content_hash == "sha256:" + action_anchor`` -- the
    anchor *is* the signed content hash, not a field bolted next to it.
    """
    return _canonical(
        {
            "prev_anchor": prev_anchor,
            "observation_hash": observation_hash,
            "action": action.to_canonical_dict(),
        }
    )


def compute_action_anchor(*, prev_anchor: str, observation_hash: str, action: Action) -> str:
    """Return the action anchor hex digest.

    ``action_anchor = sha256(canonical(prev_anchor, observation_hash, action))``.
    """
    return hashlib.sha256(
        action_anchor_preimage(prev_anchor=prev_anchor, observation_hash=observation_hash, action=action)
    ).hexdigest()


# ---------------------------------------------------------------------------
# Adapter capability check
# ---------------------------------------------------------------------------

#: Registry names of adapters that advertise computer-use capability. Kept as a
#: frozenset mirroring ``_MULTIMODAL_ADAPTERS`` in
#: :mod:`bernstein.core.agents.multimodal`.
_COMPUTER_USE_ADAPTERS: frozenset[str] = frozenset({"computer_use"})


def is_computer_use_capable(adapter_name: str) -> bool:
    """Check whether an adapter can front a browser / computer-use task.

    Mirrors :func:`bernstein.core.agents.multimodal.is_multimodal_capable`: an
    adapter that is not registered here raises a structured
    :class:`~bernstein.core.agents.computer_use_attestation.ComputerUseRefusal`
    before any process launch when handed a browser task.

    Args:
        adapter_name: The adapter name as used in the adapter registry.

    Returns:
        ``True`` when the adapter is known to accept computer-use tasks.
    """
    return adapter_name.lower() in _COMPUTER_USE_ADAPTERS


__all__ = [
    "GENESIS_ANCHOR",
    "Action",
    "ActionKind",
    "ActionObservation",
    "action_anchor_preimage",
    "compute_action_anchor",
    "compute_observation_hash",
    "digest_typed_value",
    "is_computer_use_capable",
]
