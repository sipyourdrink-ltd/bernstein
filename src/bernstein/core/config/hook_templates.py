"""Bundled command-hook templates for common integrations."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path  # noqa: TC003 — used at runtime via `/` operator


@dataclass(frozen=True)
class HookTemplateFile:
    """One file created by a bundled hook template."""

    relative_path: str
    content: str
    executable: bool = False


@dataclass(frozen=True)
class HookTemplate:
    """Description of a bundled hook template."""

    name: str
    description: str
    files: tuple[HookTemplateFile, ...]


def list_hook_templates() -> tuple[HookTemplate, ...]:
    """Return the bundled hook templates."""
    return _HOOK_TEMPLATES


def get_hook_template(name: str) -> HookTemplate | None:
    """Return one bundled hook template by name."""
    normalized = name.strip().lower()
    for template in _HOOK_TEMPLATES:
        if template.name == normalized:
            return template
    return None


def scaffold_hook_template(
    name: str,
    workdir: Path,
    *,
    force: bool = False,
) -> list[Path]:
    """Install a bundled hook template into ``workdir/.bernstein/hooks``."""
    template = get_hook_template(name)
    if template is None:
        raise ValueError(f"Unknown hook template: {name}")

    hooks_root = workdir / ".bernstein" / "hooks"
    created: list[Path] = []
    for template_file in template.files:
        destination = hooks_root / template_file.relative_path
        if destination.exists() and not force:
            raise FileExistsError(f"Hook template file already exists: {destination}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(template_file.content, encoding="utf-8")
        if template_file.executable:
            destination.chmod(0o755)
        created.append(destination)
    return created


_SLACK_README = """# slack-notify

Environment:
- `SLACK_WEBHOOK_URL`

Installed hooks:
- `on_task_failed/slack_notify.py`
- `on_task_completed/slack_notify.py`

Both scripts send a compact JSON summary to the configured Slack webhook.
"""

_SLACK_SCRIPT = """#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import sys
from urllib import request

payload = json.load(sys.stdin)
webhook_url = os.environ.get("SLACK_WEBHOOK_URL")
if not webhook_url:
    print(json.dumps({"status": "error", "message": "SLACK_WEBHOOK_URL is not set"}))
    raise SystemExit(0)

hook_name = os.path.basename(sys.argv[0]).replace(".py", "")
summary = payload.get("result_summary") or payload.get("error") or payload.get("title") or "event"
text = f"[Bernstein] {hook_name}: {payload.get('task_id', 'unknown')} ({payload.get('role', 'unknown')}) - {summary}"
body = json.dumps({"text": text}).encode("utf-8")
req = request.Request(webhook_url, data=body, headers={"Content-Type": "application/json"})
with request.urlopen(req, timeout=10) as response:
    response.read()
print(json.dumps({"status": "ok"}))
"""

_PAGERDUTY_README = """# pagerduty-alert

Environment:
- `PAGERDUTY_ROUTING_KEY`

Installed hooks:
- `on_task_failed/pagerduty_alert.py`
- `on_stop_failure/pagerduty_alert.py`

These scripts emit a PagerDuty Events v2 trigger when Bernstein reports a failure.
"""

_PAGERDUTY_SCRIPT = """#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import sys
from urllib import request

payload = json.load(sys.stdin)
routing_key = os.environ.get("PAGERDUTY_ROUTING_KEY")
if not routing_key:
    print(json.dumps({"status": "error", "message": "PAGERDUTY_ROUTING_KEY is not set"}))
    raise SystemExit(0)

summary = payload.get("error") or payload.get("reason") or "Bernstein reported a failure"
event = {
    "routing_key": routing_key,
    "event_action": "trigger",
    "payload": {
        "summary": str(summary),
        "source": "bernstein",
        "severity": "error",
        "custom_details": payload,
    },
}
body = json.dumps(event).encode("utf-8")
req = request.Request(
    "https://events.pagerduty.com/v2/enqueue",
    data=body,
    headers={"Content-Type": "application/json"},
)
with request.urlopen(req, timeout=10) as response:
    response.read()
print(json.dumps({"status": "ok"}))
"""

_JIRA_README = """# jira-update

Environment:
- `JIRA_BASE_URL`
- `JIRA_USER_EMAIL`
- `JIRA_API_TOKEN`
- optional `JIRA_ISSUE_KEY`

Installed hooks:
- `on_task_created/jira_update.py`
- `on_task_completed/jira_update.py`

The script adds a comment to the configured issue. If the hook payload already
contains `issue_key`, that value takes precedence over `JIRA_ISSUE_KEY`.
"""

_JIRA_SCRIPT = """#!/usr/bin/env python3
from __future__ import annotations

import base64
import json
import os
import sys
from urllib import request

payload = json.load(sys.stdin)
base_url = os.environ.get("JIRA_BASE_URL")
user_email = os.environ.get("JIRA_USER_EMAIL")
api_token = os.environ.get("JIRA_API_TOKEN")
issue_key = payload.get("issue_key") or os.environ.get("JIRA_ISSUE_KEY")
missing = [name for name, value in {
    "JIRA_BASE_URL": base_url,
    "JIRA_USER_EMAIL": user_email,
    "JIRA_API_TOKEN": api_token,
}.items() if not value]
if missing:
    print(json.dumps({"status": "error", "message": "Missing Jira configuration: " + ", ".join(missing)}))
    raise SystemExit(0)
if not issue_key:
    print(json.dumps({"status": "abort", "message": "No Jira issue key configured", "abort": True}))
    raise SystemExit(0)

comment = {
    "body": {
        "type": "doc",
        "version": 1,
        "content": [{
            "type": "paragraph",
            "content": [{"type": "text", "text": json.dumps(payload, sort_keys=True)}],
        }],
    }
}
auth = base64.b64encode(f"{user_email}:{api_token}".encode("utf-8")).decode("ascii")
url = base_url.rstrip("/") + f"/rest/api/3/issue/{issue_key}/comment"
req = request.Request(
    url,
    data=json.dumps(comment).encode("utf-8"),
    headers={
        "Content-Type": "application/json",
        "Authorization": f"Basic {auth}",
        "Accept": "application/json",
    },
)
with request.urlopen(req, timeout=10) as response:
    response.read()
print(json.dumps({"status": "ok"}))
"""


_HOOK_TEMPLATES: tuple[HookTemplate, ...] = (
    HookTemplate(
        name="slack-notify",
        description="Send Slack webhook notifications on task success/failure.",
        files=(
            HookTemplateFile("README.slack-notify.md", _SLACK_README),
            HookTemplateFile("on_task_failed/slack_notify.py", _SLACK_SCRIPT, executable=True),
            HookTemplateFile("on_task_completed/slack_notify.py", _SLACK_SCRIPT, executable=True),
        ),
    ),
    HookTemplate(
        name="pagerduty-alert",
        description="Trigger PagerDuty incidents for critical Bernstein failures.",
        files=(
            HookTemplateFile("README.pagerduty-alert.md", _PAGERDUTY_README),
            HookTemplateFile("on_task_failed/pagerduty_alert.py", _PAGERDUTY_SCRIPT, executable=True),
            HookTemplateFile("on_stop_failure/pagerduty_alert.py", _PAGERDUTY_SCRIPT, executable=True),
        ),
    ),
    HookTemplate(
        name="jira-update",
        description="Post Jira comments when tasks start or complete.",
        files=(
            HookTemplateFile("README.jira-update.md", _JIRA_README),
            HookTemplateFile("on_task_created/jira_update.py", _JIRA_SCRIPT, executable=True),
            HookTemplateFile("on_task_completed/jira_update.py", _JIRA_SCRIPT, executable=True),
        ),
    ),
)
