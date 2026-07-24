"""Generic JWS pass-through mandate adapter.

Projects a signed mandate to a JWS General JSON Serialization envelope
(RFC 7515 §7.2.1): the ``payload`` is the mandate's JCS-canonical signing body
and the single ``signatures[]`` entry carries the mandate's existing detached
EdDSA signature (its ``protected`` header and ``signature`` segments). This is a
scheme-agnostic bridge -- any external system that speaks JWS can consume the
envelope -- with an exact byte-identical round trip back to the native mandate.

No external payment scheme is named or blessed; this is the generic transport a
concrete out-of-tree adapter can build on.
"""

from __future__ import annotations

import base64
import json

from bernstein.core.payments.mandate import SpendMandate
from bernstein.core.security.agent_card_signer import canonicalize_jcs

__all__ = ["JwsPassthroughMandateAdapter"]


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(text: str) -> bytes:
    pad = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + pad)


class JwsPassthroughMandateAdapter:
    """Projects a mandate to/from a generic JWS General JSON Serialization blob."""

    name: str = "generic-jws"

    def to_external(self, mandate: SpendMandate) -> bytes:
        """Return a JWS General JSON envelope embedding the mandate + its signature.

        Raises:
            ValueError: When the mandate is unsigned or its signature is not a
                well-formed detached JWS.
        """
        protected, signature = _split_detached(mandate.signature)
        payload = _b64url(canonicalize_jcs(mandate._signing_body()))
        envelope = {
            "payload": payload,
            "signatures": [{"protected": protected, "signature": signature}],
        }
        return json.dumps(envelope, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")

    def from_external(self, blob: bytes) -> SpendMandate:
        """Parse a JWS General JSON envelope back into a mandate.

        Raises:
            ValueError: When the envelope is malformed or missing a signature.
        """
        try:
            envelope = json.loads(blob)
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid JWS envelope: {exc}") from exc
        if not isinstance(envelope, dict) or "payload" not in envelope:
            raise ValueError("JWS envelope missing payload")
        sigs = envelope.get("signatures")
        if not isinstance(sigs, list) or not sigs or not isinstance(sigs[0], dict):
            raise ValueError("JWS envelope missing signatures[]")
        protected = sigs[0].get("protected")
        signature = sigs[0].get("signature")
        if not isinstance(protected, str) or not isinstance(signature, str):
            raise ValueError("JWS envelope signature entry is malformed")

        try:
            body = json.loads(_b64url_decode(str(envelope["payload"])))
        except (ValueError, json.JSONDecodeError) as exc:
            raise ValueError(f"JWS payload is not valid base64url JSON: {exc}") from exc

        detached_jws = f"{protected}..{signature}"
        return SpendMandate.from_dict({**body, "signature": detached_jws})


def _split_detached(detached_jws: str) -> tuple[str, str]:
    """Return ``(protected, signature)`` from a detached JWS ``protected..signature``."""
    parts = detached_jws.split(".")
    if len(parts) != 3 or parts[1] != "":
        raise ValueError("mandate signature is not a well-formed detached JWS")
    return parts[0], parts[2]
