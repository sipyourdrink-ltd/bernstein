"""Discord trigger source — normalize Discord interaction payloads into TriggerEvents."""

from __future__ import annotations

import time
from typing import Any

from bernstein.core.models import TriggerEvent


def verify_discord_signature(body: bytes, timestamp: str, signature: str, public_key: str) -> bool:
    """Verify a Discord interaction request using Ed25519.

    Discord signs every interaction POST with an Ed25519 key. The message
    being signed is ``timestamp + body``.

    Args:
        body: Raw request body bytes.
        timestamp: Value of the ``X-Signature-Timestamp`` header.
        signature: Value of the ``X-Signature-Ed25519`` header (hex-encoded).
        public_key: Discord application public key (hex-encoded, 64 chars).

    Returns:
        True if the signature is valid, False otherwise.
    """
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

        key_bytes = bytes.fromhex(public_key)
        sig_bytes = bytes.fromhex(signature)
        message = timestamp.encode("utf-8") + body

        pk = Ed25519PublicKey.from_public_bytes(key_bytes)
        pk.verify(sig_bytes, message)
        return True
    except Exception:
        return False


def normalize_discord_interaction(payload: dict[str, Any]) -> TriggerEvent:
    """Normalize a Discord application command interaction into a TriggerEvent.

    Args:
        payload: Parsed JSON body from a Discord interaction webhook.

    Returns:
        Normalized TriggerEvent.
    """
    # Interaction data
    data: dict[str, Any] = payload.get("data", {})
    member = payload.get("member", {})
    user = payload.get("user") or member.get("user", {})

    user_id: str = user.get("id", "")
    username: str = user.get("username", "")
    command_name: str = data.get("name", "")

    # Extract option values from the interaction
    options: list[dict[str, Any]] = data.get("options", [])
    option_map: dict[str, Any] = {opt["name"]: opt.get("value", "") for opt in options}

    # Reconstruct human-readable message from the command + options
    parts = [command_name]
    for opt_name, opt_val in option_map.items():
        parts.append(f"{opt_name}={opt_val}")
    message = " ".join(parts)

    guild_id: str = payload.get("guild_id", "")
    channel_id: str = payload.get("channel_id", "")

    return TriggerEvent(
        source="discord",
        timestamp=time.time(),
        raw_payload=payload,
        sender=user_id or username,
        message=message,
        metadata={
            "command": command_name,
            "options": option_map,
            "guild_id": guild_id,
            "channel_id": channel_id,
            "username": username,
        },
    )
