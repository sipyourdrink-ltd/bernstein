"""Platform payload mappings for the automation bridge (#2512).

Deliberately thin. Each automation platform posts a slightly different envelope
shape and names its execution id in a different header; an adapter's whole job
is to unwrap that shape into a normalised trigger intent and to name the header
carrying the replay nonce. Everything that makes the bridge worth using -- the
signed receipt, the chain anchor, the deterministic graph projection, the
refusal path -- lives in :mod:`bernstein.core.trigger_sources.receipt` and is
platform-agnostic, so adding a platform is a mapping, never a protocol.

Adapters never authenticate. Authentication is the transport's job (the shared
HMAC secret on ``POST /webhook``); an adapter that could admit a trigger would
be a second, weaker gate.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

__all__ = [
    "ADAPTERS",
    "DEFAULT_PLATFORM",
    "GENERIC_TRIGGER_ID_HEADER",
    "PlatformAdapter",
    "adapter_for",
    "normalise_trigger",
    "resolve_platform",
]

#: Header any caller may set to name the trigger id explicitly. Checked before
#: the platform-specific headers so an operator can always pin the nonce.
GENERIC_TRIGGER_ID_HEADER = "x-bernstein-trigger-id"

#: Platform label used when no adapter matches the request.
DEFAULT_PLATFORM = "generic"

_TITLE_KEYS = ("title", "summary", "name", "goal", "subject")
_DESCRIPTION_KEYS = ("description", "body", "details", "text", "message")
_ROLE_KEYS = ("role", "agent_role", "worker_role")
_PRIORITY_KEYS = ("priority", "urgency")
_STEP_KEYS = ("steps", "tasks", "items")


@dataclass(frozen=True)
class PlatformAdapter:
    """Mapping from one automation platform's payload shape to a trigger intent.

    Attributes:
        platform: The platform label recorded on the receipt.
        envelope_keys: Keys the platform may nest the useful payload under.
            Tried in order; the first mapping value found is unwrapped.
        trigger_id_headers: Headers the platform sets its execution id in,
            most specific first.
        scope: The scope granted to a trigger admitted through this adapter.
    """

    platform: str
    envelope_keys: tuple[str, ...] = ()
    trigger_id_headers: tuple[str, ...] = ()
    scope: str = "task:create"

    def unwrap(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        """Return the useful payload, unwrapping the platform's envelope."""
        for key in self.envelope_keys:
            nested = payload.get(key)
            if isinstance(nested, Mapping):
                return nested
        return payload

    def trigger_id(self, headers: Mapping[str, str]) -> str:
        """Return the replay nonce for this request, or an empty string.

        Args:
            headers: Request headers; matched case-insensitively.
        """
        lowered = {str(k).lower(): str(v) for k, v in headers.items()}
        for name in (GENERIC_TRIGGER_ID_HEADER, *self.trigger_id_headers):
            value = lowered.get(name, "").strip()
            if value:
                return value
        return ""

    def intent(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Return the normalised trigger intent this payload expresses."""
        inner = self.unwrap(payload)
        intent: dict[str, Any] = {
            "title": _first_str(inner, _TITLE_KEYS),
            "description": _first_str(inner, _DESCRIPTION_KEYS),
        }
        role = _first_str(inner, _ROLE_KEYS)
        if role:
            intent["role"] = role
        priority = _first_str(inner, _PRIORITY_KEYS)
        if priority:
            intent["priority"] = priority
        steps = _first_steps(inner)
        if steps:
            intent["steps"] = steps
        return intent


#: The bridge's platform adapters. Each is a payload mapping and a header name;
#: the receipt and anchor core below them is shared and platform-agnostic.
ADAPTERS: dict[str, PlatformAdapter] = {
    "n8n": PlatformAdapter(
        platform="n8n",
        # The Webhook node forwards the original request under ``body``; an
        # HTTP Request node posts the fields at the top level.
        envelope_keys=("body", "json"),
        trigger_id_headers=("x-n8n-execution-id", "x-n8n-signature", "webhook-id"),
    ),
    "zapier": PlatformAdapter(
        platform="zapier",
        # "Webhooks by Zapier" posts flat fields; Code steps often nest under
        # ``data``.
        envelope_keys=("data",),
        trigger_id_headers=("x-zapier-request-id", "x-request-id"),
    ),
    "workato": PlatformAdapter(
        platform="workato",
        # The HTTP connector posts the recipe's mapped object under ``input``.
        envelope_keys=("input", "payload"),
        trigger_id_headers=("x-workato-job-id", "x-workato-request-id"),
    ),
    DEFAULT_PLATFORM: PlatformAdapter(
        platform=DEFAULT_PLATFORM,
        trigger_id_headers=("webhook-id", "x-request-id"),
    ),
}


def adapter_for(platform: str) -> PlatformAdapter:
    """Return the adapter for ``platform``, falling back to the generic one."""
    return ADAPTERS.get(platform.strip().lower(), ADAPTERS[DEFAULT_PLATFORM])


def resolve_platform(headers: Mapping[str, str]) -> str:
    """Return the platform label a request identifies itself as.

    An explicit ``X-Bernstein-Platform`` header wins. Otherwise the first
    adapter whose platform-specific header is present claims the request, and
    the generic adapter takes what is left. The label is recorded on the
    receipt, not trusted for authentication.
    """
    lowered = {str(k).lower(): str(v) for k, v in headers.items()}
    declared = lowered.get("x-bernstein-platform", "").strip().lower()
    if declared in ADAPTERS:
        return declared
    for name, adapter in ADAPTERS.items():
        if name == DEFAULT_PLATFORM:
            continue
        if any(header in lowered for header in adapter.trigger_id_headers if header != "webhook-id"):
            return name
    return DEFAULT_PLATFORM


def normalise_trigger(
    *,
    payload: Mapping[str, Any],
    headers: Mapping[str, str],
    platform: str = "",
) -> tuple[str, dict[str, Any], str]:
    """Return ``(platform, intent, trigger_id)`` for one inbound request.

    Args:
        payload: The decoded request body.
        headers: The request headers.
        platform: Explicit platform label; resolved from headers when omitted.

    Returns:
        The resolved platform label, the normalised trigger intent, and the
        replay nonce (empty when the caller supplied none).
    """
    resolved = platform.strip().lower() if platform else resolve_platform(headers)
    adapter = adapter_for(resolved)
    return adapter.platform, adapter.intent(payload), adapter.trigger_id(headers)


def _first_str(payload: Mapping[str, Any], keys: Sequence[str]) -> str:
    """Return the first key's value coerced to a non-empty string."""
    for key in keys:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return str(value)
    return ""


def _first_steps(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Return the first list-valued step key, normalised to mappings."""
    for key in _STEP_KEYS:
        value = payload.get(key)
        if not isinstance(value, list) or not value:
            continue
        steps: list[dict[str, Any]] = []
        for item in value:
            if isinstance(item, Mapping):
                steps.append(
                    {
                        "title": _first_str(item, _TITLE_KEYS),
                        "description": _first_str(item, _DESCRIPTION_KEYS),
                        "role": _first_str(item, _ROLE_KEYS),
                        "priority": _first_str(item, _PRIORITY_KEYS),
                    }
                )
            else:
                steps.append({"title": str(item), "description": "", "role": "", "priority": ""})
        return steps
    return []
