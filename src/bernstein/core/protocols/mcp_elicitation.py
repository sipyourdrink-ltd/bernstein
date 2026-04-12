"""MCP-008: MCP elicitation handling.

Server-initiated prompts (elicitations) forwarded to user or auto-resolved
based on configurable policies.

MCP servers may send ``elicitation/create`` requests asking the client
(Bernstein) for input -- e.g. confirming a destructive action, selecting
a branch, or providing credentials.  This module:

1. Receives elicitation requests from MCP servers.
2. Matches them against auto-resolve policies (pattern-based).
3. If no policy matches, queues them for user resolution.
4. Returns the resolution (user-provided or auto) to the server.

Usage::

    from bernstein.core.protocols.mcp_elicitation import ElicitationHandler

    handler = ElicitationHandler()
    handler.add_auto_policy("confirm_delete", pattern="confirm.*delete", response="yes")
    result = handler.handle(ElicitationRequest(
        id="e1", server_name="github", message="Confirm delete branch?",
        schema={"type": "string"}, request_type="confirmation",
    ))
"""

from __future__ import annotations

import logging
import re
import time
import uuid
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

logger = logging.getLogger(__name__)


class ElicitationStatus(StrEnum):
    """Status of an elicitation request."""

    PENDING = "pending"
    AUTO_RESOLVED = "auto_resolved"
    USER_RESOLVED = "user_resolved"
    TIMED_OUT = "timed_out"
    DENIED = "denied"


@dataclass
class ElicitationRequest:
    """An elicitation request from an MCP server.

    Attributes:
        id: Unique request identifier.
        server_name: MCP server that sent the elicitation.
        message: Human-readable prompt message.
        schema: JSON Schema describing the expected response shape.
        request_type: Category (e.g. "confirmation", "input", "selection").
        created_at: Unix timestamp of creation.
        status: Current resolution status.
        response: The resolved response value.
        resolved_by: Who/what resolved it ("auto:policy_name" or "user").
        resolved_at: Unix timestamp of resolution.
    """

    id: str = ""
    server_name: str = ""
    message: str = ""
    schema: dict[str, Any] = field(default_factory=dict)  # type: ignore[reportUnknownVariableType]
    request_type: str = "input"
    created_at: float = field(default_factory=time.time)
    status: ElicitationStatus = ElicitationStatus.PENDING
    response: Any = None
    resolved_by: str = ""
    resolved_at: float = 0.0

    def __post_init__(self) -> None:
        if not self.id:
            self.id = uuid.uuid4().hex[:12]

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a JSON-compatible dict."""
        return {
            "id": self.id,
            "server_name": self.server_name,
            "message": self.message,
            "request_type": self.request_type,
            "status": self.status.value,
            "response": self.response,
            "resolved_by": self.resolved_by,
            "created_at": self.created_at,
            "resolved_at": self.resolved_at,
        }


@dataclass(frozen=True)
class AutoResolvePolicy:
    """A policy for automatically resolving elicitation requests.

    Attributes:
        name: Policy name (for logging/debugging).
        pattern: Regex pattern matched against the elicitation message.
        response: The automatic response value to return.
        request_types: If non-empty, only match these request types.
        server_names: If non-empty, only match these server names.
    """

    name: str
    pattern: str
    response: Any
    request_types: tuple[str, ...] = ()
    server_names: tuple[str, ...] = ()

    def matches(self, request: ElicitationRequest) -> bool:
        """Return True if this policy matches the request."""
        if self.request_types and request.request_type not in self.request_types:
            return False
        if self.server_names and request.server_name not in self.server_names:
            return False
        return bool(re.search(self.pattern, request.message, re.IGNORECASE))


class ElicitationHandler:
    """Handles MCP elicitation requests with auto-resolve policies.

    Args:
        default_timeout: Seconds before a pending request times out.
    """

    def __init__(self, *, default_timeout: float = 300.0) -> None:
        self._policies: list[AutoResolvePolicy] = []
        self._pending: dict[str, ElicitationRequest] = {}
        self._resolved: list[ElicitationRequest] = []
        self._default_timeout = default_timeout

    def add_auto_policy(
        self,
        name: str,
        *,
        pattern: str,
        response: Any,
        request_types: tuple[str, ...] = (),
        server_names: tuple[str, ...] = (),
    ) -> None:
        """Register an auto-resolve policy.

        Args:
            name: Policy name.
            pattern: Regex pattern to match against elicitation messages.
            response: Auto-response value.
            request_types: If set, only match these request types.
            server_names: If set, only match these server names.
        """
        policy = AutoResolvePolicy(
            name=name,
            pattern=pattern,
            response=response,
            request_types=request_types,
            server_names=server_names,
        )
        self._policies.append(policy)
        logger.debug("Registered auto-resolve policy '%s' (pattern=%s)", name, pattern)

    def handle(self, request: ElicitationRequest) -> ElicitationRequest:
        """Process an elicitation request.

        Checks auto-resolve policies first. If none match, the request is
        queued as pending for user resolution.

        Args:
            request: The elicitation request from an MCP server.

        Returns:
            The request with updated status and response.
        """
        for policy in self._policies:
            if policy.matches(request):
                request.status = ElicitationStatus.AUTO_RESOLVED
                request.response = policy.response
                request.resolved_by = f"auto:{policy.name}"
                request.resolved_at = time.time()
                self._resolved.append(request)
                logger.info(
                    "Auto-resolved elicitation '%s' from '%s' via policy '%s'",
                    request.id,
                    request.server_name,
                    policy.name,
                )
                return request

        request.status = ElicitationStatus.PENDING
        self._pending[request.id] = request
        logger.info(
            "Elicitation '%s' from '%s' queued as pending: %s",
            request.id,
            request.server_name,
            request.message,
        )
        return request

    def resolve(self, request_id: str, response: Any) -> ElicitationRequest | None:
        """Resolve a pending elicitation request with a user-provided response.

        Args:
            request_id: The elicitation request ID.
            response: The user's response.

        Returns:
            The resolved request, or None if not found.
        """
        request = self._pending.pop(request_id, None)
        if request is None:
            return None
        request.status = ElicitationStatus.USER_RESOLVED
        request.response = response
        request.resolved_by = "user"
        request.resolved_at = time.time()
        self._resolved.append(request)
        logger.info("User resolved elicitation '%s'", request_id)
        return request

    def deny(self, request_id: str) -> ElicitationRequest | None:
        """Deny a pending elicitation request.

        Args:
            request_id: The elicitation request ID.

        Returns:
            The denied request, or None if not found.
        """
        request = self._pending.pop(request_id, None)
        if request is None:
            return None
        request.status = ElicitationStatus.DENIED
        request.resolved_by = "user"
        request.resolved_at = time.time()
        self._resolved.append(request)
        logger.info("User denied elicitation '%s'", request_id)
        return request

    def expire_timed_out(self) -> list[ElicitationRequest]:
        """Expire pending requests that have exceeded the timeout.

        Returns:
            List of expired requests.
        """
        now = time.time()
        expired: list[ElicitationRequest] = []
        for req_id, request in list(self._pending.items()):
            if (now - request.created_at) > self._default_timeout:
                request.status = ElicitationStatus.TIMED_OUT
                request.resolved_at = now
                self._resolved.append(request)
                expired.append(request)
                del self._pending[req_id]
        return expired

    def get_pending(self) -> list[ElicitationRequest]:
        """Return all pending elicitation requests."""
        return list(self._pending.values())

    def get_resolved(self) -> list[ElicitationRequest]:
        """Return all resolved elicitation requests."""
        return list(self._resolved)

    def to_dict(self) -> dict[str, Any]:
        """Serialize handler state to a JSON-compatible dict."""
        return {
            "pending": [r.to_dict() for r in self._pending.values()],
            "resolved_count": len(self._resolved),
            "policy_count": len(self._policies),
        }
