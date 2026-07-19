"""Wire an :class:`A2AReceiptIssuer` into the running task server (#2609).

This is the small amount of glue between the pure receipt machinery in
:mod:`bernstein.core.protocols.a2a.receipt` and the server's on-disk layout.
It resolves the three inputs an issuer needs -

* a :class:`~bernstein.core.lineage.spine.LineageSpine` under
  ``.sdd/lineage``, on its own run id,
* the operator HMAC key (shared with the rest of the audit chain), and
* the node's Ed25519 signing identity,

- and hands back a ready issuer, or ``None`` when the environment cannot
support one.

Failing soft is deliberate. A node that cannot mint receipts should still
answer A2A traffic; it simply answers *unattested*, and the absent ``receipt``
field is the signal a caller checks. Refusing to serve would trade a
verifiability gap for an availability outage, which is the worse failure for
an interop surface.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

    from bernstein.core.protocols.a2a.receipt import A2AReceiptIssuer

logger = logging.getLogger(__name__)

__all__ = ["build_receipt_issuer"]

#: Actor recorded on spine entries for inbound A2A responses.
_A2A_ACTOR = "a2a-server"

#: Dedicated spine run id. One run keeps inbound A2A responses from
#: interleaving with per-task run journals, so the A2A chain can be verified
#: (and handed to a reviewer) on its own.
_A2A_RUN_ID = "a2a-inbound-responses"

#: Stable ``kid`` for the signing identity behind receipt head signatures.
_A2A_LINEAGE_KID = "a2a-server-lineage-1"

#: Key file names under ``<sdd_dir>/a2a/identity``.
_PRIVATE_KEY_NAME = "a2a_lineage.pem"
_PUBLIC_KEY_NAME = "a2a_lineage.pub"


def build_receipt_issuer(sdd_dir: Path) -> A2AReceiptIssuer | None:
    """Return an issuer rooted at ``sdd_dir``, or ``None`` if unavailable.

    Args:
        sdd_dir: The server's ``.sdd`` directory. Lineage entries land under
            ``<sdd_dir>/lineage``; the signing identity under
            ``<sdd_dir>/a2a/identity``.

    Returns:
        A configured :class:`A2AReceiptIssuer`, or ``None`` when key material
        or the lineage root cannot be provisioned.
    """
    try:
        from bernstein.core.lineage.identity import load_or_create_signing_identity
        from bernstein.core.lineage.spine import LineageSpine
        from bernstein.core.protocols.a2a.receipt import A2AReceiptIssuer
        from bernstein.core.security.audit import load_or_create_audit_key
        from bernstein.core.security.lineage_kms import FileBasedKMSAdapter

        identity_dir = sdd_dir / "a2a" / "identity"
        load_or_create_signing_identity(
            identity_dir,
            private_name=_PRIVATE_KEY_NAME,
            public_name=_PUBLIC_KEY_NAME,
        )

        # The head-signature signer reuses the lineage identity's private key,
        # so a verifier that already trusts this node's lineage key needs no
        # second trust root for receipts. Point the adapter at the file the
        # helper already wrote at 0o600 rather than copying key material.
        key_path = identity_dir / _PRIVATE_KEY_NAME

        return A2AReceiptIssuer(
            spine=LineageSpine(
                sdd_dir / "lineage",
                run_id=_A2A_RUN_ID,
                hmac_key=load_or_create_audit_key(),
            ),
            kid=_A2A_LINEAGE_KID,
            kms_adapter=FileBasedKMSAdapter(key_path, kid=_A2A_LINEAGE_KID),
            actor=_A2A_ACTOR,
        )
    except Exception as exc:  # pragma: no cover - environment-dependent
        # Serve unattested rather than not at all; the missing receipt is the
        # signal, and it is louder than a 500.
        logger.warning("A2A receipt issuer unavailable, responses will be unattested: %s", exc)
        return None
