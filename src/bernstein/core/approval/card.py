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
    """Return the canonical JSON string for *payload* (sorted, compact)."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


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
        return {"tool_name": self.tool_name, "args_digest": self.args_digest}

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
            "score": self.score,
            "hard_one_way": self.hard_one_way,
            "rationale": self.rationale,
            "fired_detectors": list(self.fired_detectors),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ImpactEstimate:
        return cls(
            score=float(data.get("score", 0.0)),
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
        return {"procedure": self.procedure, "irreversible": self.irreversible}

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
        """Return the canonical envelope dict.

        Floats are coerced explicitly so a JSON round-trip through the audit
        chain rehydrates byte-identical values and the hash stays stable.
        """
        return {
            "approval_id": self.approval_id,
            "action": self.action.to_dict(),
            "reasoning": self.reasoning,
            "impact": self.impact.to_dict(),
            "rollback": self.rollback.to_dict(),
            "created_at": self.created_at,
            "not_after": self.not_after,
            "card_version": self.card_version,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ApprovalCardV2:
        """Rebuild an envelope from its canonical dict (round-trips ``to_dict``)."""
        return cls(
            approval_id=str(data.get("approval_id", "")),
            action=ActionRef.from_dict(dict(data.get("action", {}))),
            reasoning=str(data.get("reasoning", "")),
            impact=ImpactEstimate.from_dict(dict(data.get("impact", {}))),
            rollback=RollbackPlan.from_dict(dict(data.get("rollback", {}))),
            created_at=float(data.get("created_at", 0.0)),
            not_after=float(data.get("not_after", 0.0)),
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


def rollback_for(tool_name: str, tool_args: dict[str, Any], *, irreversible: bool) -> RollbackPlan:
    """Return a rollback plan for a tool call, keyed by tool class.

    When *irreversible* is ``True`` (a ``hard_one_way`` detector fired) the
    plan is prefixed with an explicit irreversible marker and carries the
    ``irreversible`` flag, both of which land inside the hashed envelope.
    """
    name = tool_name.strip().lower()
    path = ""
    for field_name in _PATH_FIELDS:
        value = tool_args.get(field_name)
        if isinstance(value, str) and value:
            path = value
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
        ttl_seconds: Lifetime; ``not_after = created_at + ttl_seconds``.
        report: Optional precomputed blast-radius report. When ``None`` the
            scorer is run over the derived change set.
        detectors: Optional detector override forwarded to the scorer.

    Returns:
        The canonical envelope. Two calls with identical inputs (and identical
        detector set / repository state) return byte-identical envelopes.
    """
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
        created_at=float(created_at),
        not_after=float(created_at) + float(ttl_seconds),
    )


# ---------------------------------------------------------------------------
# Verbatim rendering (shared by every driver)
# ---------------------------------------------------------------------------


def render_card_text(card: ApprovalCardV2) -> str:
    """Render the envelope as operator-facing text, verbatim from hashed fields.

    Every driver calls this instead of re-summarising, so the displayed text
    is a pure projection of the hashed envelope: if any field the operator saw
    differed from what was hashed, the echoed ``card_hash`` would not match.
    The final line prints the ``card_hash`` itself so the operator (and any
    verifier) can confirm the message equals the committed record.
    """
    impact = card.impact
    if impact.fired_detectors:
        detector_note = f"; fired detectors: {', '.join(impact.fired_detectors)}"
    else:
        detector_note = "; no detectors fired"
    lines = [
        f"Action: {card.action.tool_name}",
        f"Args digest: {card.action.args_digest}",
        f"Intent: {card.reasoning or '(none provided)'}",
        f"Impact: score {impact.score:.2f}{detector_note}",
    ]
    if impact.hard_one_way or card.rollback.irreversible:
        lines.append("IRREVERSIBLE ACTION: change tripped a one-way-door blast-radius detector.")
    lines.extend(
        (
            f"Rollback: {card.rollback.procedure}",
            f"Expires at: {card.not_after:.0f} (enforced by the audit chain, not this chat client)",
            f"Card hash: {card_hash(card)}",
        )
    )
    return "\n".join(lines)
