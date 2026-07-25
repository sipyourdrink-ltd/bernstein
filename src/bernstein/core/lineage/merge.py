"""Fork resolution + steward merge entry construction (ADR-009 §6.3).

A merge entry is a lineage record with `parent_hashes` of length >= 2 that
points to the resolved siblings of one fork. It carries the steward's
`agent_id` + `agent_card_kid`, the `content_hash` of the resolved content,
and the operator HMAC. The choice of WHICH sibling's content wins is made
by a `MergePolicy`:

  - `HumanPolicy` (`"human"`)        - default; raises `LineageConflict`
                                       so the operator can run the CLI.
  - `FirstWriterPolicy`              - earliest `ts_ns` wins; agent_id lex
                                       tiebreak.
  - `AgentPolicy("agent:<id>")`      - designated agent's tip always wins.

Steward privilege is enforced by allow-listing at gate time, not by the
shape of the entry (same key type as workers).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from bernstein.core.lineage.entry import LineageEntry, canonicalise, compute_operator_hmac
from bernstein.core.lineage.identity import AgentCard, sign_detached

if TYPE_CHECKING:
    from bernstein.core.lineage.tips import Fork


class LineageConflict(Exception):
    """Raised when a merge cannot be resolved without operator input."""

    def __init__(self, artefact_path: str, candidate_hashes: tuple[str, ...], reason: str) -> None:
        super().__init__(f"{reason} (artefact={artefact_path}, candidates={candidate_hashes})")
        self.artefact_path = artefact_path
        self.candidate_hashes = candidate_hashes
        self.reason = reason


@runtime_checkable
class MergePolicy(Protocol):
    """Pick the winning entry for one fork."""

    def resolve(self, fork: Fork, by_hash: dict[str, LineageEntry]) -> LineageEntry:
        """Return the winning child entry. Raise `LineageConflict` if undecidable."""
        ...


@dataclass(frozen=True, slots=True)
class HumanPolicy:
    """Default. Always raises `LineageConflict` for operator-driven resolution."""

    def resolve(self, fork: Fork, by_hash: dict[str, LineageEntry]) -> LineageEntry:
        raise LineageConflict(
            artefact_path=fork.artefact_path,
            candidate_hashes=fork.child_hashes,
            reason="merge policy is 'human' - operator must choose",
        )


@dataclass(frozen=True, slots=True)
class FirstWriterPolicy:
    """Earliest ts_ns wins; agent_id lex order tiebreak."""

    def resolve(self, fork: Fork, by_hash: dict[str, LineageEntry]) -> LineageEntry:
        children = [by_hash[h] for h in fork.child_hashes]
        children.sort(key=lambda e: (e.ts_ns, e.agent_id))
        return children[0]


@dataclass(frozen=True, slots=True)
class AgentPolicy:
    """Designated agent's tip wins. Raises if no candidate matches."""

    agent_id: str

    def resolve(self, fork: Fork, by_hash: dict[str, LineageEntry]) -> LineageEntry:
        matches = [by_hash[h] for h in fork.child_hashes if by_hash[h].agent_id == self.agent_id]
        if not matches:
            raise LineageConflict(
                artefact_path=fork.artefact_path,
                candidate_hashes=fork.child_hashes,
                reason=f"no tip from designated agent {self.agent_id!r}",
            )
        # Latest write from the designated agent wins.
        matches.sort(key=lambda e: e.ts_ns, reverse=True)
        return matches[0]


def resolve_policy(policy_name: str) -> MergePolicy:
    """Construct a `MergePolicy` from a config-string name."""
    if policy_name == "human":
        return HumanPolicy()
    if policy_name == "first-writer":
        return FirstWriterPolicy()
    if policy_name.startswith("agent:"):
        # Strip the leading "agent:" once; if the agent_id itself starts with
        # "agent:" the remainder is the canonical slug, otherwise we re-prepend.
        rest = policy_name[len("agent:") :]
        agent_id = rest if rest.startswith("agent:") else f"agent:{rest}"
        return AgentPolicy(agent_id=agent_id)
    raise ValueError(f"unknown merge policy: {policy_name!r}")


@dataclass(frozen=True, slots=True)
class StewardKey:
    """Steward's signing material - Agent Card + Ed25519 private key PEM."""

    card: AgentCard
    private_key_pem: str
    operator_secret: bytes = b""


def _content_hash(content: bytes) -> str:
    return "sha256:" + hashlib.sha256(content).hexdigest()


def build_merge_entry(
    forks: list[Fork],
    *,
    resolved_content_by_path: dict[str, bytes],
    steward: StewardKey,
    now_ns: int,
    tool_call_id: str = "tc-steward-merge",
    span_id: str = "00steward",
) -> list[tuple[LineageEntry, str]]:
    """Construct one merge entry per fork.

    The merge entry's ``operator_hmac`` covers the JCS-canonical bytes of the
    entry with the ``operator_hmac`` field blanked - identical to the scheme
    used by :func:`bernstein.core.lineage.signed_write.seal_write` so the
    CI gate accepts both kinds of entry under the same operator secret.

    Returns a list of `(entry, jws)` pairs. Caller is responsible for appending
    them to the lineage log via `LineageStore.append`.
    """
    out: list[tuple[LineageEntry, str]] = []
    for fork in forks:
        content = resolved_content_by_path[fork.artefact_path]
        unsigned_entry = LineageEntry(
            v=1,
            artefact_path=fork.artefact_path,
            artefact_kind="file",
            content_hash=_content_hash(content),
            parent_hashes=list(fork.child_hashes),
            agent_id=steward.card.agent_id,
            agent_card_kid=steward.card.kid,
            tool_call_id=tool_call_id,
            span_id=span_id,
            ts_ns=now_ns,
            operator_hmac="",
        )
        op_hmac = compute_operator_hmac(unsigned_entry, steward.operator_secret)
        entry = LineageEntry(
            v=1,
            artefact_path=fork.artefact_path,
            artefact_kind="file",
            content_hash=_content_hash(content),
            parent_hashes=list(fork.child_hashes),
            agent_id=steward.card.agent_id,
            agent_card_kid=steward.card.kid,
            tool_call_id=tool_call_id,
            span_id=span_id,
            ts_ns=now_ns,
            operator_hmac=op_hmac,
        )
        canonical = canonicalise(entry)
        jws = sign_detached(canonical, steward.private_key_pem, kid=steward.card.kid)
        out.append((entry, jws))
    return out


__all__ = [
    "AgentPolicy",
    "FirstWriterPolicy",
    "HumanPolicy",
    "LineageConflict",
    "MergePolicy",
    "StewardKey",
    "build_merge_entry",
    "resolve_policy",
]
