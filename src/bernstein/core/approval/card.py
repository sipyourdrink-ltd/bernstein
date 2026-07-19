"""Approval card v2: a hash-committed, chain-anchored decision record.

Issue #2511. The v1 chat approval card carried only ``(title, body,
thread_id)``: the attested event proved *who* approved *which* tool call but
not *what decision context the operator was shown*. The v2 card closes that
gap by turning the card into a canonical, hashable decision record.

The envelope (:class:`ApprovalCardV2`) carries, all inside one hashed
structure:

* the action -- the tool name plus a canonical digest of its arguments,
* a bounded reasoning digest -- the agent's stated intent,
* an impact estimate -- the blast-radius score plus the fired-detector
  rationale from :func:`bernstein.core.quality.blast_radius.score_change`,
* a rollback procedure -- a per-tool-class template, carrying an explicit
  ``irreversible`` marker whenever a ``hard_one_way`` detector fired,
* a ``not_after`` expiry.

:func:`card_hash` computes the SHA-256 over the canonical JSON of the
envelope (sorted keys, compact separators). Two issuances of the same
pending approval against identical repository state produce byte-identical
envelopes and an identical ``card_hash`` -- the card is a deterministic
projection of its inputs, not a re-summarisation. Drivers render the
envelope fields verbatim (see :func:`render_card_text`) so what is displayed
is exactly what was hashed.

This module is intentionally free of any transport, chat, or audit-chain
imports: it only depends on the blast-radius scorer and the stdlib, so the
envelope can be built and hashed from anywhere -- CLI, gate, MCP router --
before the orchestrator is bootstrapped.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from bernstein.core.quality.blast_radius import score_change

if TYPE_CHECKING:
    from collections.abc import Sequence

    from bernstein.core.quality.blast_radius import BlastRadiusReport, Detector

__all__ = [
    "CARD_VERSION",
    "REASONING_MAX_CHARS",
    "ActionRef",
    "ApprovalCardV2",
    "ImpactEstimate",
    "RollbackPlan",
    "args_digest",
    "build_card",
    "canonical_card_bytes",
    "card_hash",
    "impact_from_tool_call",
    "render_card_envelope",
    "render_card_text",
    "rollback_for",
]

#: Schema marker embedded in every envelope. Bumping it changes ``card_hash``
#: for every card, so a verifier can tell v2 envelopes apart from any future
#: revision without guessing from field presence.
CARD_VERSION = "v2"

#: The reasoning digest is bounded so a runaway intent string cannot bloat the
#: envelope (and the audit chain). The bound is part of the contract: the same
#: over-long intent always truncates to the same bytes, keeping the hash
#: deterministic.
REASONING_MAX_CHARS = 600


def _canonical_dumps(payload: dict[str, Any]) -> str:
    """Return the canonical JSON string for *payload* (sorted, compact).

    ``allow_nan`` is disabled: ``NaN`` / ``Infinity`` are JavaScript literals,
    not JSON, so permitting them would let an envelope hash over bytes no
    conforming parser can read back. A non-finite value therefore raises here
    rather than producing an envelope that only round-trips through Python.
    """
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


#: Characters that would end a rendered line and let an interpolated value
#: forge additional ``Label: value`` rows. U+2028 and U+2029 are included
#: because several chat and web renderers break lines on them even though
#: ``str.splitlines`` is not what does the breaking.
_LINE_BREAKS = {
    "\\": "\\\\",
    "\n": "\\n",
    "\r": "\\r",
    "\u2028": "\\u2028",
    "\u2029": "\\u2029",
}


def _single_line(text: str) -> str:
    """Return *text* with line separators escaped, so it cannot forge a card row.

    :func:`render_card_text` emits one ``Label: value`` row per line, so any
    interpolated value containing a line break can synthesise rows of its own.
    ``reasoning`` is supplied by the agent -- exactly the party the approval
    card exists to constrain -- so an unescaped newline lets it render a
    complete, benign-looking card, ending in a forged ``Card hash:`` line,
    above the true fields.

    The escape is injective (the backslash is escaped first), so the displayed
    form still determines the hashed value exactly and an operator can recover
    it. Escaping happens at render time only: the envelope keeps the raw value,
    so no ``card_hash`` moves.
    """
    for raw, escaped in _LINE_BREAKS.items():
        text = text.replace(raw, escaped)
    return text


def _require_finite(value: object, *, field: str) -> float:
    """Return *value* as a finite ``float``, rejecting anything else.

    This is a validation boundary: *value* may come straight from JSON stored on
    the audit chain, so it is typed as ``object`` and narrowed here rather than
    trusted.

    Two things are load-bearing:

    * **Non-finite rejection.** Non-finite timestamps are not merely malformed,
      they are exploitable: every comparison against ``NaN`` is ``False``, so a
      ``NaN`` ``not_after`` makes ``now >= not_after`` permanently false and the
      card never expires chain-side.
    * **Widening to ``float``.** An ``int`` must not survive into the envelope.
      ``1000`` serialises as ``1000`` while ``1000.0`` serialises as ``1000.0``,
      so an integer timestamp would produce different canonical bytes and a
      different ``card_hash`` for what is the same instant.

    ``bool`` is rejected explicitly: it is an ``int`` subclass, and a ``True``
    timestamp silently meaning ``1.0`` is never what the caller meant.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        msg = f"{field} must be a finite number, got {value!r}"
        raise ValueError(msg)
    widened = float(value)
    if not math.isfinite(widened):
        msg = f"{field} must be a finite number, got {value!r}"
        raise ValueError(msg)
    return widened


def args_digest(tool_args: dict[str, Any]) -> str:
    """Return the SHA-256 hex digest of *tool_args* in canonical form.

    The digest binds exactly the arguments the tool would run with, so a
    decision echoing the card commits to the concrete invocation and not just
    the tool name.
    """
    return hashlib.sha256(_canonical_dumps(tool_args.copy()).encode("utf-8")).hexdigest()


def _bound_reasoning(reasoning: str) -> str:
    """Return *reasoning* trimmed to :data:`REASONING_MAX_CHARS` characters.

    The bound is applied deterministically so identical intent text always
    yields identical envelope bytes.
    """
    text = reasoning.strip()
    if len(text) <= REASONING_MAX_CHARS:
        return text
    return text[: REASONING_MAX_CHARS - 1].rstrip() + "…"


@dataclass(frozen=True, slots=True)
class ActionRef:
    """The concrete tool invocation the card gates."""

    tool_name: str
    args_digest: str

    def to_dict(self) -> dict[str, Any]:
        return {"tool_name": str(self.tool_name), "args_digest": str(self.args_digest)}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ActionRef:
        return cls(
            tool_name=str(data.get("tool_name", "")),
            args_digest=str(data.get("args_digest", "")),
        )


@dataclass(frozen=True, slots=True)
class ImpactEstimate:
    """Quantified blast-radius impact surfaced on the card.

    Attributes:
        score: Blast-radius score in ``[0, 1]``.
        hard_one_way: ``True`` when a ``hard_one_way`` detector fired, i.e. the
            change contains a one-way door (schema migration, secrets write,
            ``rm -rf``, ``DROP``/``DELETE`` SQL, ...).
        rationale: The scorer's structured rationale string.
        fired_detectors: Ordered ids of every detector that fired.
    """

    score: float
    hard_one_way: bool
    rationale: str
    fired_detectors: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "score": float(self.score),
            "hard_one_way": bool(self.hard_one_way),
            "rationale": str(self.rationale),
            "fired_detectors": [str(d) for d in self.fired_detectors],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ImpactEstimate:
        """Rebuild an impact estimate, rejecting a non-finite score.

        ``score`` is validated for the same reason the timestamps are: a
        non-finite value reaching the envelope makes :func:`card_hash` raise
        (canonical JSON refuses ``NaN``), and a raise escaping a verifier is a
        denial-of-audit primitive rather than a detection.
        """
        return cls(
            score=_require_finite(data.get("score", 0.0), field="impact.score"),
            hard_one_way=bool(data.get("hard_one_way", False)),
            rationale=str(data.get("rationale", "")),
            fired_detectors=tuple(str(d) for d in data.get("fired_detectors", [])),
        )


@dataclass(frozen=True, slots=True)
class RollbackPlan:
    """How to undo the action if the approval turns out wrong.

    Attributes:
        procedure: Operator-facing text describing the undo path.
        irreversible: ``True`` when the change tripped a one-way door and no
            clean automatic rollback exists. When ``True`` the card renders an
            explicit irreversible marker, and because this flag lives inside
            the hashed envelope the marker is cryptographically committed.
    """

    procedure: str
    irreversible: bool

    def to_dict(self) -> dict[str, Any]:
        return {"procedure": str(self.procedure), "irreversible": bool(self.irreversible)}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RollbackPlan:
        return cls(
            procedure=str(data.get("procedure", "")),
            irreversible=bool(data.get("irreversible", False)),
        )


@dataclass(frozen=True, slots=True)
class ApprovalCardV2:
    """The canonical, hashable approval decision record.

    Every field is operator-visible and lives inside the hashed envelope, so
    :func:`card_hash` commits to the entire decision context: intent,
    quantified impact, undo path, and a real deadline. A decision that echoes
    the ``card_hash`` therefore commits to exactly what was displayed; a card
    that merely displays extra text (without hashing it) cannot satisfy that.
    """

    approval_id: str
    action: ActionRef
    reasoning: str
    impact: ImpactEstimate
    rollback: RollbackPlan
    created_at: float
    not_after: float
    card_version: str = CARD_VERSION

    def to_dict(self) -> dict[str, Any]:
        """Return the canonical envelope dict, in its persisted normal form.

        Every field is coerced to exactly the type :meth:`from_dict` produces,
        which makes ``to_dict`` a fixed point of the storage round-trip:
        ``to_dict(from_dict(to_dict(x))) == to_dict(x)`` for any constructible
        card, whatever types the constructor happened to receive.

        This is what the hash is taken over, so the hash commits to the bytes
        that actually get persisted rather than to the in-memory object. The
        distinction is not cosmetic. ``card_hash`` is recomputed from stored
        JSON on two paths (gate rehydration after a restart, and the offline
        verifier), and JSON does not preserve Python's numeric types: an
        ``int`` ``0`` serialises as ``0`` while the ``float`` that
        :meth:`from_dict` rebuilds serialises as ``0.0``. Coercing only on the
        way in would leave those two disagreeing, and an honest card would
        become unresolvable after a restart and fail ``audit verify``
        permanently on an append-only chain.

        Normalising here rather than validating inputs is deliberate: input
        validation has to be re-tightened every time a new caller or a new
        field appears, whereas a normal form holds for all of them at once.
        """
        return {
            "approval_id": str(self.approval_id),
            "action": self.action.to_dict(),
            "reasoning": str(self.reasoning),
            "impact": self.impact.to_dict(),
            "rollback": self.rollback.to_dict(),
            "created_at": float(self.created_at),
            "not_after": float(self.not_after),
            "card_version": str(self.card_version),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ApprovalCardV2:
        """Rebuild an envelope from its canonical dict (round-trips ``to_dict``).

        Timestamps are validated on the way in: an envelope rehydrated from the
        chain with a non-finite ``created_at`` / ``not_after`` is rejected here
        rather than silently producing a card that can never expire.
        """
        return cls(
            approval_id=str(data.get("approval_id", "")),
            action=ActionRef.from_dict(dict(data.get("action", {}))),
            reasoning=str(data.get("reasoning", "")),
            impact=ImpactEstimate.from_dict(dict(data.get("impact", {}))),
            rollback=RollbackPlan.from_dict(dict(data.get("rollback", {}))),
            created_at=_require_finite(data.get("created_at", 0.0), field="created_at"),
            not_after=_require_finite(data.get("not_after", 0.0), field="not_after"),
            card_version=str(data.get("card_version", CARD_VERSION)),
        )

    def is_expired(self, *, now: float) -> bool:
        """Return ``True`` when *now* is at or past the envelope's ``not_after``."""
        return now >= self.not_after


def canonical_card_bytes(card: ApprovalCardV2) -> bytes:
    """Return the canonical bytes hashed into ``card_hash``."""
    return _canonical_dumps(card.to_dict()).encode("utf-8")


def card_hash(card: ApprovalCardV2) -> str:
    """Return the SHA-256 hex digest committing to the whole envelope."""
    return hashlib.sha256(canonical_card_bytes(card)).hexdigest()


# ---------------------------------------------------------------------------
# Impact estimation (blast radius)
# ---------------------------------------------------------------------------

# Argument fields that name a file the tool would touch.
_PATH_FIELDS = ("file_path", "path", "notebook_path", "target")
# Argument fields whose value is content / a command we can scan with the
# content_regex detectors (rm -rf, DROP TABLE, secrets writes, ...).
_CONTENT_FIELDS = (
    "content",
    "new_string",
    "new_str",
    "command",
    "shell_cmd",
    "code",
    "body",
    "diff",
)


def _derive_change(tool_name: str, tool_args: dict[str, Any]) -> tuple[list[str], str]:
    """Derive ``(files, diff_text)`` for the blast-radius scorer from a call."""
    del tool_name  # kept for future per-tool overrides
    files: list[str] = []
    for field_name in _PATH_FIELDS:
        value = tool_args.get(field_name)
        if isinstance(value, str) and value:
            files.append(value)
            break
    diff_parts: list[str] = []
    for field_name in _CONTENT_FIELDS:
        value = tool_args.get(field_name)
        if isinstance(value, str) and value:
            diff_parts.append(value)
    return files, "\n".join(diff_parts)


def impact_from_tool_call(
    tool_name: str,
    tool_args: dict[str, Any],
    *,
    detectors: Sequence[Detector] | None = None,
) -> BlastRadiusReport:
    """Score the blast radius of a tool call.

    The change set is derived from well-known argument fields: the first path
    field names the touched file, and content / command fields feed the
    content detectors so ``rm -rf``, ``DROP``/``DELETE`` SQL and secrets
    writes surface as one-way doors.
    """
    files, diff_text = _derive_change(tool_name, tool_args)
    return score_change(files=files, diff_text=diff_text, detectors=detectors)


# ---------------------------------------------------------------------------
# Rollback templates (per tool class)
# ---------------------------------------------------------------------------

_WRITE_TOOLS = frozenset(
    {"write", "edit", "multiedit", "notebookedit", "str_replace", "str_replace_editor", "create_file", "apply_patch"}
)
_SHELL_TOOLS = frozenset({"bash", "shell", "run", "execute", "exec", "command"})
_NET_TOOLS = frozenset({"webfetch", "websearch", "fetch", "http", "curl"})

_IRREVERSIBLE_PREFIX = "IRREVERSIBLE: this change tripped a one-way-door detector; there is no clean automatic undo. "

#: The rollback template embeds the touched path twice, so an unbounded path is
#: the one field that can push a rendered card past a chat driver's body cap
#: (Discord 2000, Slack 3000) -- and no driver chunks, so an oversized card is
#: not truncated, it fails to deliver. Bounding the path keeps the highest
#: blast-radius approvals deliverable. The bound is part of the contract, like
#: :data:`REASONING_MAX_CHARS`: the same path always truncates to the same
#: bytes, so the envelope stays deterministic and the truncated form is what
#: gets hashed and displayed.
ROLLBACK_PATH_MAX_CHARS = 160


def _bound_path(path: str) -> str:
    """Return *path* trimmed to :data:`ROLLBACK_PATH_MAX_CHARS`, keeping the tail.

    The tail is kept because the filename and its immediate parents identify the
    target far better than the leading directories do.
    """
    if len(path) <= ROLLBACK_PATH_MAX_CHARS:
        return path
    return "..." + path[-(ROLLBACK_PATH_MAX_CHARS - 3) :]


def rollback_for(tool_name: str, tool_args: dict[str, Any], *, irreversible: bool) -> RollbackPlan:
    """Return a rollback plan for a tool call, keyed by tool class.

    When *irreversible* is ``True`` (a ``hard_one_way`` detector fired) the
    plan is prefixed with an explicit irreversible marker and carries the
    ``irreversible`` flag, both of which land inside the hashed envelope.

    The touched path is bounded (see :data:`ROLLBACK_PATH_MAX_CHARS`) so the
    rendered card stays deliverable. The full arguments remain committed through
    ``action.args_digest``, so nothing is lost from the proof.
    """
    name = tool_name.strip().lower()
    path = ""
    for field_name in _PATH_FIELDS:
        value = tool_args.get(field_name)
        if isinstance(value, str) and value:
            path = _bound_path(value)
            break

    if name in _WRITE_TOOLS:
        target = f" `{path}`" if path else " the affected file"
        body = (
            f"Restore the prior contents of{target} from version control "
            f"(for example `git checkout HEAD --{(' ' + path) if path else ' <path>'}`), "
            "or discard the worktree change before it is committed."
        )
    elif name in _SHELL_TOOLS:
        body = (
            "Shell effects are not auto-reversible: review the command's side effects and "
            "undo them manually (restore deleted paths from backup or version control, "
            "revert created resources)."
        )
    elif name in _NET_TOOLS:
        body = "Read-only network access has no state to roll back; no undo step is required."
    else:
        body = (
            "No per-tool rollback template is registered for this tool class; inspect the "
            "tool's effects and undo them from version control or backup before relying on them."
        )

    if irreversible:
        return RollbackPlan(procedure=_IRREVERSIBLE_PREFIX + body, irreversible=True)
    return RollbackPlan(procedure=body, irreversible=False)


# ---------------------------------------------------------------------------
# Envelope factory
# ---------------------------------------------------------------------------


def build_card(
    *,
    approval_id: str,
    tool_name: str,
    tool_args: dict[str, Any],
    reasoning: str,
    created_at: float,
    ttl_seconds: float,
    report: BlastRadiusReport | None = None,
    detectors: Sequence[Detector] | None = None,
) -> ApprovalCardV2:
    """Build a canonical :class:`ApprovalCardV2` from a pending tool call.

    Args:
        approval_id: The approval id the card is issued for.
        tool_name: Tool being invoked.
        tool_args: Arguments the tool would run with (digested into the card).
        reasoning: The agent's stated intent (bounded into the envelope).
        created_at: Issue time in unix epoch seconds. Passed explicitly so the
            envelope is a pure function of its inputs (determinism).
        ttl_seconds: Lifetime; ``not_after = created_at + ttl_seconds``. Must be
            finite and non-negative.
        report: Optional precomputed blast-radius report. When ``None`` the
            scorer is run over the derived change set.
        detectors: Optional detector override forwarded to the scorer.

    Returns:
        The canonical envelope. Two calls with identical inputs (and identical
        detector set / repository state) return byte-identical envelopes.

    Raises:
        ValueError: When *created_at* or *ttl_seconds* is non-finite, or when
            *ttl_seconds* is negative (which would issue a card that is already
            expired at the instant it is built).
    """
    issued_at = _require_finite(created_at, field="created_at")
    lifetime = _require_finite(ttl_seconds, field="ttl_seconds")
    if lifetime < 0.0:
        msg = f"ttl_seconds must be non-negative, got {ttl_seconds!r}"
        raise ValueError(msg)
    expires_at = _require_finite(issued_at + lifetime, field="not_after")
    effective = report if report is not None else impact_from_tool_call(tool_name, tool_args, detectors=detectors)
    impact = ImpactEstimate(
        score=effective.score,
        hard_one_way=effective.hard_one_way,
        rationale=effective.rationale,
        fired_detectors=tuple(hit.detector_id for hit in effective.hits),
    )
    rollback = rollback_for(tool_name, tool_args, irreversible=effective.hard_one_way)
    return ApprovalCardV2(
        approval_id=approval_id,
        action=ActionRef(tool_name=tool_name, args_digest=args_digest(tool_args)),
        reasoning=_bound_reasoning(reasoning),
        impact=impact,
        rollback=rollback,
        created_at=issued_at,
        not_after=expires_at,
    )


# ---------------------------------------------------------------------------
# Verbatim rendering (shared by every driver)
# ---------------------------------------------------------------------------


def render_card_text(card: ApprovalCardV2) -> str:
    """Render the envelope as operator-facing text, verbatim from hashed fields.

    Every driver calls this instead of re-summarising, so the displayed text is
    a *complete* projection of the hashed envelope rather than a summary of it.
    Completeness is what makes the echoed ``card_hash`` meaningful: a render
    that dropped or rounded a hashed field would let two different envelopes
    display identically, so an operator could approve a card whose committed
    bytes differed from the bytes they read.

    Two properties are therefore load-bearing:

    * **Every hashed field appears.** ``approval_id``, ``card_version``, the
      action, the full impact estimate (including the rationale and the
      ``hard_one_way`` flag), the rollback plan and both timestamps.
    * **Values round-trip.** Floats are rendered with :func:`repr`, which is
      exact for IEEE-754 doubles, instead of a truncating format. A score of
      ``0.6449`` no longer displays as ``0.64``.

    The final line carries the ``card_hash``. Because the lines above already
    reproduce every hashed field losslessly, an operator can rebuild the
    envelope from what they read and confirm it hashes to that value.

    Field values have their line separators escaped (see :func:`_single_line`).
    One row per line is what makes the projection readable, and that is exactly
    what an agent-supplied value containing a newline would exploit: it could
    otherwise render a complete forged card, ending in a fake ``Card hash:``
    row, above the true fields. The escape is injective, so losslessness is
    preserved.

    The canonical JSON envelope is deliberately *not* inlined here. Duplicating
    every field a second time as JSON more than doubled the body and pushed
    large cards past the Discord (2000) and Slack (3000) character caps, and no
    driver chunks, so the approval card for exactly the highest-blast-radius
    operations would fail to deliver. Callers that need the exact canonical
    bytes use :func:`render_card_envelope`.
    """
    impact = card.impact
    if impact.fired_detectors:
        detector_note = f"fired detectors: {', '.join(_single_line(d) for d in impact.fired_detectors)}"
    else:
        detector_note = "no detectors fired"
    lines = [
        f"Card version: {_single_line(card.card_version)}",
        f"Approval id: {_single_line(card.approval_id)}",
        f"Action: {_single_line(card.action.tool_name)}",
        f"Args digest: {_single_line(card.action.args_digest)}",
        f"Intent: {_single_line(card.reasoning)}",
        f"Impact: score {impact.score!r}; hard_one_way={impact.hard_one_way}; {detector_note}",
        f"Impact rationale: {_single_line(impact.rationale)}",
    ]
    if impact.hard_one_way or card.rollback.irreversible:
        lines.append("IRREVERSIBLE ACTION: change tripped a one-way-door blast-radius detector.")
    lines.extend(
        (
            f"Rollback: {_single_line(card.rollback.procedure)}",
            f"Rollback irreversible: {card.rollback.irreversible}",
            f"Created at: {card.created_at!r}",
            f"Expires at: {card.not_after!r} (enforced by the audit chain, not this chat client)",
            f"Card hash: {card_hash(card)}",
        )
    )
    return "\n".join(lines)


def render_card_envelope(card: ApprovalCardV2) -> str:
    """Return the canonical JSON envelope with its ``card_hash``, for verification.

    This is the exact byte sequence that was hashed, so a verifier can re-hash
    it and compare. It is kept out of :func:`render_card_text` because inlining
    it in a chat body doubles the message length and pushes large cards past the
    Discord and Slack limits; surface it where length is not constrained (a CLI
    lookup, a file attachment, a details pane).
    """
    return f"{_canonical_dumps(card.to_dict())}\nCard hash: {card_hash(card)}"
