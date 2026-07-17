"""Declarative payload-to-event templates for inbound webhooks (#2548).

The generic inbound webhook path accepts a new external system's POST through
configuration instead of code: a declarative template maps JSON payload paths to
canonical event fields. The discipline is content-addressing before rendering -
the raw payload bytes are hashed into the chain first, so a render is always
reproducible from the recorded bytes, and a render failure emits a diagnostic
feed event carrying only the payload digest, never the payload.

Admission verification and trigger receipts remain the surface of #2512; this
layer only turns an already-admitted payload into a canonical feed event.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, cast


def content_address(payload: bytes) -> str:
    """Return the ``sha256:`` content address of raw payload bytes."""
    return "sha256:" + hashlib.sha256(payload).hexdigest()


class TemplatePathError(KeyError):
    """Raised internally when a required payload path is absent."""


def _extract(payload: Any, dot_path: str) -> Any:
    """Resolve a dot-delimited path against a decoded JSON payload.

    Raises:
        TemplatePathError: When any segment is missing.
    """
    node: Any = payload
    for segment in dot_path.split("."):
        if isinstance(node, dict) and segment in node:
            node = cast("dict[str, Any]", node)[segment]
        else:
            raise TemplatePathError(dot_path)
    return node


@dataclass(frozen=True, slots=True)
class WebhookTemplate:
    """A declarative payload-to-event mapping.

    Attributes:
        template_id: Stable identifier recorded in anchors and diagnostics.
        label: The canonical label the rendered event carries.
        resource_path: Dot path to the value used as ``resource_id`` (required).
        related_paths: Dot paths to lineage ancestor ids; absent paths are
            skipped, so an optional relation does not fail the render.
    """

    template_id: str
    label: str
    resource_path: str
    related_paths: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class WebhookRenderResult:
    """Outcome of rendering a payload against a template.

    Attributes:
        ok: Whether rendering succeeded.
        payload_digest: The content address of the raw payload bytes. Present on
            success and failure alike - it is the only trace of the payload a
            failure ever records.
        event: The rendered canonical event mapping on success, else ``None``.
        error_kind: A short machine token on failure (``invalid_json`` /
            ``missing_resource``), else ``None``.
    """

    ok: bool
    payload_digest: str
    event: dict[str, Any] | None = None
    error_kind: str | None = None
    related_resource_ids: tuple[str, ...] = field(default_factory=tuple)


def render(template: WebhookTemplate, payload_bytes: bytes) -> WebhookRenderResult:
    """Render ``payload_bytes`` into a canonical event mapping, deterministically.

    The payload digest is computed from the raw bytes first, so it is stable
    regardless of whether the render then succeeds. Re-rendering the same bytes
    yields a byte-identical event mapping. A malformed payload or a missing
    required path yields a failure carrying the digest and an error token, and
    never any payload content.
    """
    digest = content_address(payload_bytes)
    try:
        decoded = json.loads(payload_bytes)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return WebhookRenderResult(ok=False, payload_digest=digest, error_kind="invalid_json")

    try:
        resource_raw = _extract(decoded, template.resource_path)
    except TemplatePathError:
        return WebhookRenderResult(ok=False, payload_digest=digest, error_kind="missing_resource")

    related: set[str] = set()
    for path in template.related_paths:
        try:
            value = _extract(decoded, path)
        except TemplatePathError:
            continue
        if isinstance(value, str) and value:
            related.add(value)
    related_ids = tuple(sorted(related))

    event = {
        "label": template.label,
        "resource_id": str(resource_raw),
        "related_resource_ids": list(related_ids),
        "payload_digest": digest,
        "template_id": template.template_id,
    }
    return WebhookRenderResult(
        ok=True,
        payload_digest=digest,
        event=event,
        related_resource_ids=related_ids,
    )


def canonical_event_json(event: dict[str, Any]) -> str:
    """Return the byte-stable canonical JSON of a rendered event mapping."""
    return json.dumps(event, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


__all__ = [
    "WebhookRenderResult",
    "WebhookTemplate",
    "canonical_event_json",
    "content_address",
    "render",
]
