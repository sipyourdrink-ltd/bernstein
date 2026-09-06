#!/usr/bin/env python3
"""Re-mint the agent-card UTF-16-ordering vector in this directory (issue #5551).

Run by hand from a source checkout, never by the test suite::

    uv run python tests/fixtures/agent-card-utf16-vector/_build_agent_card_utf16_vector.py

Importing this module builds nothing: everything happens inside
:func:`build`, behind a ``__main__`` guard, so a test can import it and call
it against a temporary directory without overwriting the committed files.

Why this vector, and not a hand-written dict
---------------------------------------------
RFC 8785 sorts object property names as UTF-16 code units, which disagrees
with the obvious code-point shortcut in exactly one case: a supplementary-plane
name (above U+FFFF) against a name in U+E000..U+FFFF. Every record this
codebase actually emits today has schema-fixed ASCII keys, so that disagreement
never fires in production and a shortcut implementation would pass every
corpus we have.

``tests/unit/test_canonicalize_jcs_key_order.py`` already proves the
canonicaliser itself gets this right, against hand-built dicts. What is
missing -- and what a hand-built dict cannot supply -- is a record that came
out of a real signing path and still exercises the split, so an independent
verifier checking this codebase's actual output (not a fixture written for
the occasion) can tell the two orderings apart.

``AgentIdentityCard.extensions`` (``dict[str, str | bool | int | float]``) is
the open-membership field: no schema restricts its key names to the
"recognised keys today" the field's docstring lists, so a caller can
legitimately set a key that is any string, and ``sign_agent_card`` /
``verify_agent_card`` are the production functions that canonicalise and sign
the whole card body via ``agent_card_signer.canonicalize_jcs``.

Determinism
-----------
Every input is fixed: the signing key is derived from a pinned seed, every
timestamp on the card is a constant, and JCS plus Ed25519 are deterministic
given deterministic inputs. Running this twice must produce byte-identical
output, which
``test_regenerating_the_fixture_is_byte_identical_to_the_committed_files``
enforces.

The signing key is a test key, published in this directory alongside the
vector it signs. It is not, and must never become, an installation identity.
"""

from __future__ import annotations

import hashlib
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "src"))

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from bernstein.core.identity.agent_card import AgentIdentityCard
from bernstein.core.security.agent_card_signer import canonicalize_jcs, sign_agent_card

#: Deterministic signing seed the vector was minted under. A test-only key,
#: published alongside the vector it signs.
_SIGN_SEED = b"c" * 32

#: Key identifier carried in the JWS protected header.
_KID = "install-cardfixture01"

#: Fixture clock. Every timestamp on the card is a constant, never wall-clock.
_CREATED_AT = 1700000000.0
_EXPIRES_AT = 1700086400.0

#: The two property names the issue measures, verbatim: U+FFFF (BMP, above
#: the surrogate range) sorts *below* U+1D11E (supplementary-plane, whose
#: UTF-16 lead surrogate is U+D834) in code-point order, and *above* it in
#: UTF-16 code-unit order -- the one case the two orderings disagree.
_BMP_KEY = "￿"
_SUPPLEMENTARY_KEY = "\U0001d11e"

_CARD_NAME = "agent-card-utf16-vector.json"
_DIGEST_NAME = "agent-card-utf16-vector.sha256"
_SIGNATURE_NAME = "agent-card-utf16-vector-signature.json"
_KEY_NAME = "agent-card-utf16-vector-key.pem"


def _private_key() -> Ed25519PrivateKey:
    return Ed25519PrivateKey.from_private_bytes(_SIGN_SEED)


def _private_key_pem() -> bytes:
    return _private_key().private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )


def _card() -> AgentIdentityCard:
    """Return the fixture card: schema-legitimate, with a disagreeing key pair.

    ``extensions`` is where a caller sets free-form flags, so the two keys
    that make the ordering split observable sit there rather than on any
    fixed-name field. ``task_budgets`` is included as an ordinary recognised
    key so the fixture is not degenerate -- it looks like a real card that
    happens to also carry the two keys under test.
    """
    return AgentIdentityCard(
        agent_id="agent-fixture-utf16",
        role="backend",
        adapter="claude",
        model="claude-fixture-model",
        capabilities=["repo.read", "repo.write"],
        max_budget_usd=5.0,
        extensions={
            "task_budgets": True,
            _BMP_KEY: True,
            _SUPPLEMENTARY_KEY: False,
        },
        created_at=_CREATED_AT,
        expires_at=_EXPIRES_AT,
    )


def build(dest: Path) -> Path:
    """Write the card body, its digest, the signature, and the public key.

    Returns:
        The path of the written card-body file.
    """
    dest.mkdir(parents=True, exist_ok=True)
    card = _card()

    private_key_pem = _private_key_pem()
    signature = sign_agent_card(card, private_key_pem, kid=_KID)

    # ``asdict(card)`` matches ``agent_card_signer._card_to_dict`` exactly
    # (that private helper is nothing more than this call) -- using it
    # directly here avoids reaching across a module boundary for a name
    # that isn't part of the public signing API.
    card_bytes = canonicalize_jcs(asdict(card))
    card_path = dest / _CARD_NAME
    card_path.write_bytes(card_bytes)

    (dest / _DIGEST_NAME).write_text(
        f"{hashlib.sha256(card_bytes).hexdigest()}  {_CARD_NAME}\n",
        encoding="utf-8",
    )

    signature_payload: dict[str, Any] = {
        "detached_jws": signature.detached_jws,
        "kid": signature.kid,
        "alg": signature.alg,
    }
    (dest / _SIGNATURE_NAME).write_bytes(canonicalize_jcs(signature_payload) + b"\n")

    (dest / _KEY_NAME).write_bytes(
        _private_key().public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )
    return card_path


if __name__ == "__main__":
    written = build(Path(__file__).resolve().parent)
    print(f"wrote {written}")
