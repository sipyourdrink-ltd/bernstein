"""Draft proposal mechanism for ``govern discover --assist`` (issue #5020, #4981).

This module records the model-synthesized draft playbook as a ``DraftProposal``
artifact that ``govern apply`` refuses to execute until a human has signed it.

The property that makes this ours:

- Its **input** is a findings document derived entirely from chain-recorded
  observations. The model never touches the environment.
- Its **output** is a proposal, recorded as a DraftProposal, that
  ``govern apply`` refuses to execute until a human has signed it — the
  property ``#4981`` already establishes.
- The findings document it reads is **content-addressed and on the chain**, so an
  operator reviewing the draft six weeks later can answer "what did it actually
  see?" without re-running anything.
- The prompt is on the chain, so the draft is explicable (test 6 acceptance).

No model sits in the coordination loop. It drafts text; the scheduler stays
deterministic.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import Enum
from typing import Any


class ProposalStatus(Enum):
    """Status of a draft playbook proposal."""

    DRAFT = "draft"
    SIGNED = "signed"
    REJECTED = "rejected"


@dataclass(frozen=True, slots=True)
class DraftProposal:
    """A proposal for a draft playbook, recorded as an artifact that
    ``govern apply`` refuses to execute until a human has signed it.

    This is the core artifact of ``govern discover --assist`` (issue #5020,
    #4981). The model produces a draft playbook from a findings document, and
    the resulting DraftProposal is recorded on the chain. ``govern apply`` will
    not apply the plan until a human signs it.

    The DraftProposal binds the draft to its source findings document via a
    content hash, and records the prompt that was sent to the model so that the
    draft is reproducible and explicable.

    Attributes:
        findings_hash: The ``sha256:``-prefixed content hash of the
            FindingsDocument this proposal was derived from. Binds the proposal
            to its source inventory for offline auditability.
        prompt: The prompt that was sent to the model to generate the draft
            playbook. Kept on the chain so that ``test_same_findings_document_and_same_seed_produce_a_recorded_prompt``
            (acceptance test 6) can verify reproducibility.
        playbook: The drafted playbook as a dict, following the playbook schema:
            ``{"forbidden": [...], "required": [...], "permitted": [...]}``.
        model: The model name that generated this draft.
        timestamp: Integer timestamp when the proposal was created.
        status: Current status: ``ProposalStatus.DRAFT`` (unsigned), ``ProposalStatus.SIGNED`` (human-signed),
            or ``ProposalStatus.REJECTED``.
        human_signature: An optional HMAC signature from a human operator. When
            set (non-None), ``govern apply`` will execute the plan. When None,
            ``govern apply`` refuses to execute.
    """

    findings_hash: str
    prompt: str
    playbook: dict[str, Any]
    model: str
    timestamp: int
    status: ProposalStatus
    human_signature: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Return the canonical serialization."""
        return {
            "findings_hash": self.findings_hash,
            "prompt": self.prompt,
            "playbook": self.playbook,
            "model": self.model,
            "timestamp": self.timestamp,
            "status": self.status.value,
            "human_signature": self.human_signature,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> DraftProposal:
        """Rebuild a DraftProposal from a serialized dict."""
        return cls(
            findings_hash=str(raw["findings_hash"]),
            prompt=str(raw["prompt"]),
            playbook=raw.get("playbook", {}),
            model=str(raw.get("model", "")),
            timestamp=int(raw["timestamp"]),
            status=ProposalStatus(str(raw["status"])),
            human_signature=raw.get("human_signature"),
        )

    def to_canonical_bytes(self) -> bytes:
        """Serialize the DraftProposal to canonical JSON bytes.

        The canonical form uses sorted keys, minimal separators, and UTF-8
        encoding. This is the form hashed into the lineage spine, so two
        replays against the same inputs produce byte-identical artifacts.
        """
        return json.dumps(
            self.to_dict(),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")

    def content_hash(self) -> str:
        """Return the ``sha256:``-prefixed content address of this proposal."""
        return "sha256:" + hashlib.sha256(self.to_canonical_bytes()).hexdigest()

    def is_signed(self) -> bool:
        """Return True if this proposal has been signed by a human operator.

        When ``is_signed()`` is True, ``govern apply`` will execute the plan.
        When False, ``govern apply`` refuses to execute.
        """
        return self.status == ProposalStatus.SIGNED and self.human_signature is not None

    def sign(self, signature: str) -> DraftProposal:
        """Return a new DraftProposal signed with the given human signature.

        Args:
            signature: The human operator's signature (e.g., an HMAC hex string).

        Returns:
            A new DraftProposal with status=ProposalStatus.SIGNED and
            human_signature set to the given signature.
        """
        return DraftProposal(
            findings_hash=self.findings_hash,
            prompt=self.prompt,
            playbook=self.playbook,
            model=self.model,
            timestamp=self.timestamp,
            status=ProposalStatus.SIGNED,
            human_signature=signature,
        )


__all__ = [
    "DraftProposal",
    "ProposalStatus",
]
