"""Signed engagement scope grants for security-tool actions (issue #2952)."""

from bernstein.core.security.engagement.mandate import (
    ENGAGEMENT_SCHEMA_VERSION,
    EngagementMandate,
    ScopeDecision,
    ScopeDenyReason,
    check_scope,
)

__all__ = [
    "ENGAGEMENT_SCHEMA_VERSION",
    "EngagementMandate",
    "ScopeDecision",
    "ScopeDenyReason",
    "check_scope",
]
