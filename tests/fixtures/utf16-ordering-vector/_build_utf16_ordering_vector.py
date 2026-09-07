#!/usr/bin/env python3
"""Re-mint the UTF-16 key-ordering vector in this directory (issue #5551).

Run by hand from a source checkout, never by the test suite::

    uv run python tests/fixtures/utf16-ordering-vector/_build_utf16_ordering_vector.py

It signs an ``AgentIdentityCard`` whose ``extensions`` map carries two keys
chosen so that UTF-16 code-unit order and Unicode code-point order disagree,
using the production signing path, and writes the card, its signature and its
public key.

Why the record comes out of the real signer
-------------------------------------------
A vector a codebase writes for itself only demonstrates that the codebase
agrees with itself. The property under test is that an *independent* RFC 8785
implementation computes the same signing bytes we do, so the record has to be
one we genuinely emit: ``sign_agent_card`` over ``_card_to_dict``, the same
path any signed card takes.

``extensions`` is used because it is the one open-membership map on the card.
Every other signed field has a schema-fixed ASCII name, which is exactly why
this property was previously unfalsifiable from outside: our own corpus could
not express a key that exercises it. No schema was changed to admit these
keys; the field is already ``dict[str, str | bool | int | float]``.

Determinism
-----------
Unlike the audit-receipt vectors, this one is fully reproducible: the key is
pinned below, and ``created_at``/``expires_at`` are pinned rather than taken
from the wall clock. Re-running this script produces byte-identical files, and
``tests/unit/test_utf16_key_ordering_vector.py`` asserts that.
"""

from __future__ import annotations

import json
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519

from bernstein.core.identity.agent_card import AgentIdentityCard
from bernstein.core.security.agent_card_signer import (
    _card_to_dict,
    ed25519_public_jwk,
    sign_agent_card,
)

HERE = Path(__file__).resolve().parent

#: U+FFFF: BMP, one UTF-16 code unit, FFFF.
BMP_KEY = "\uffff"
#: U+1D11E MUSICAL SYMBOL G CLEF: supplementary plane, surrogate pair D834 DD1E.
SUPPLEMENTARY_KEY = "\U0001D11E"

#: A fixed Ed25519 seed, so the vector is byte-reproducible. Test key only.
SEED = bytes(range(32))

#: Pinned so re-minting does not move the bytes.
CREATED_AT = 1_767_225_600.0
EXPIRES_AT = 1_798_761_600.0


def build_card() -> AgentIdentityCard:
    return AgentIdentityCard(
        agent_id="utf16-ordering-vector",
        role="reviewer",
        adapter="claude",
        model="claude-opus-5",
        extensions={
            # Deliberately inserted in code-point order, so the canonical
            # output cannot accidentally match insertion order.
            BMP_KEY: "bmp-u-ffff",
            SUPPLEMENTARY_KEY: "supplementary-u-1d11e",
            "plain": "ascii-control",
        },
        created_at=CREATED_AT,
        expires_at=EXPIRES_AT,
    )


def main() -> None:
    private_key = ed25519.Ed25519PrivateKey.from_private_bytes(SEED)
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public_pem = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )

    card = build_card()
    signature = sign_agent_card(card, private_pem, kid="utf16-ordering-vector")

    (HERE / "card.json").write_text(
        json.dumps(_card_to_dict(card), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (HERE / "signature.json").write_text(
        json.dumps(
            {"detached_jws": signature.detached_jws, "kid": signature.kid},
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    (HERE / "public-key.pem").write_bytes(public_pem)
    (HERE / "public-key.jwk.json").write_text(
        json.dumps(ed25519_public_jwk(public_pem, kid="utf16-ordering-vector"), indent=2)
        + "\n",
        encoding="utf-8",
    )
    print("wrote card.json, signature.json, public-key.pem, public-key.jwk.json")


if __name__ == "__main__":
    main()
