"""GitHub App integration for Bernstein.

Receives GitHub webhooks, converts events to Bernstein tasks, and posts
them to the task server. Provides webhook verification, event parsing,
and event-to-task mapping.
"""

from __future__ import annotations

from bernstein.github_app.app import GitHubAppConfig, create_installation_token
from bernstein.github_app.mapper import (
    issue_to_tasks,
    label_to_action,
    pr_review_to_task,
    push_to_tasks,
)
from bernstein.github_app.webhooks import WebhookEvent, parse_webhook, verify_signature

__all__ = [
    "GitHubAppConfig",
    "WebhookEvent",
    "create_installation_token",
    "issue_to_tasks",
    "label_to_action",
    "parse_webhook",
    "pr_review_to_task",
    "push_to_tasks",
    "verify_signature",
]
