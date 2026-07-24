"""Operator signing identity for mandates and receipts.

Both the mandate signature and the lineage ``.jws`` sidecar on every receipt are
produced with one operator identity: the Ed25519 keypair the install already
persists for agent-card federation
(:class:`bernstein.core.security.agent_card_keystore.AgentCardKeystore`). Reusing
that keystore is deliberate -- the feature introduces no new key surface, so
there is exactly one operator key to protect and rotate.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from bernstein.core.lineage.identity import AgentCard
from bernstein.core.payments.mandate import mandate_kid
from bernstein.core.security.agent_card_keystore import AgentCardKeystore

if TYPE_CHECKING:
    from pathlib import Path

__all__ = ["OperatorIdentity", "load_operator_identity"]

#: Stable agent id recorded on the lineage entries a receipt produces.
OPERATOR_AGENT_ID: str = "payment-mandate-operator"


@dataclass(frozen=True, slots=True)
class OperatorIdentity:
    """The operator's Ed25519 identity for signing mandates and receipts."""

    private_pem: str
    public_pem: str
    kid: str

    @property
    def agent_card(self) -> AgentCard:
        """Return an :class:`AgentCard` view used by the lineage recorder."""
        return AgentCard(agent_id=OPERATOR_AGENT_ID, kid=self.kid, public_key_pem=self.public_pem)


def load_operator_identity(keystore_dir: Path) -> OperatorIdentity:
    """Load (or first-run create) the operator signing identity from the keystore.

    Args:
        keystore_dir: Directory backing the agent-card keystore (typically
            ``<workdir>/.bernstein/keys``).

    Returns:
        The operator identity: private PEM (for signing), public PEM (embedded
        in mandates and receipts for offline verify), and a deterministic kid.
    """
    private_pem, public_pem = AgentCardKeystore(keystore_dir).load_or_generate()
    return OperatorIdentity(
        private_pem=private_pem.decode("ascii"),
        public_pem=public_pem.decode("ascii"),
        kid=mandate_kid(public_pem),
    )
