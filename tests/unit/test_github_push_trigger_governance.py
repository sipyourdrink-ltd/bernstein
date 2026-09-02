"""POST /webhooks/github push events are governed by triggers.yaml (#4545).

`triggers.yaml` promises rule-governed event handling - cooldowns, dedup,
glob filters - but the production `POST /webhooks/github` route built its QA
verification task directly, bypassing `TriggerManager` entirely. A cooldown
or dedup rule authored for `source: github_push` was silently ignored.

These tests drive the real HTTP route with signed fixture push events, not
`TriggerManager.evaluate()` called directly, so they prove the gate is wired
into production task creation rather than just implemented in isolation.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path
from typing import Any

import pytest
import yaml
from httpx import ASGITransport, AsyncClient

from bernstein.core.orchestration.trigger_manager import TriggerManager
from bernstein.core.server import create_app

_WEBHOOK_SECRET = "gh-push-governance-secret"


def _sign(body: bytes, secret: str = _WEBHOOK_SECRET) -> str:
    digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def _push_body(
    *,
    ref: str = "refs/heads/main",
    after: str = "abc123def456",
    sender: str = "alice",
    message: str = "fix: tidy up",
) -> bytes:
    payload: dict[str, Any] = {
        "ref": ref,
        "after": after,
        "commits": [{"id": after, "message": message, "added": [], "modified": ["src/app.py"], "removed": []}],
        "repository": {"full_name": "acme/widgets"},
        "sender": {"login": sender},
    }
    return json.dumps(payload).encode()


@pytest.fixture()
def sdd_dir(tmp_path: Path) -> Path:
    return tmp_path / ".sdd"


@pytest.fixture()
def app(sdd_dir: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", _WEBHOOK_SECRET)
    jsonl_path = sdd_dir / "tasks" / "tasks.jsonl"
    jsonl_path.parent.mkdir(parents=True, exist_ok=True)
    return create_app(jsonl_path=jsonl_path)


@pytest.fixture()
async def client(app) -> AsyncClient:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


def _write_triggers_config(sdd_dir: Path, *, cooldown_s: int = 300, branch: str = "main") -> None:
    config = {
        "triggers": [
            {
                "name": "push-cooldown",
                "source": "github_push",
                "enabled": True,
                "filters": {"branches": [branch]},
                "conditions": {"cooldown_s": cooldown_s},
                "task": {"title": "Governed push task"},
            },
        ],
    }
    config_path = sdd_dir / "config" / "triggers.yaml"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(yaml.dump(config))


async def _post_push(client: AsyncClient, **kw: Any) -> Any:
    body = _push_body(**kw)
    headers = {"x-hub-signature-256": _sign(body), "x-github-event": "push"}
    return await client.post("/webhooks/github", content=body, headers=headers)


@pytest.mark.anyio
async def test_cooldown_rule_suppresses_second_push_within_window(client: AsyncClient, sdd_dir: Path) -> None:
    """AC1: a cooldown rule on github_push suppresses the second push in-window."""
    _write_triggers_config(sdd_dir, cooldown_s=300)

    first = await _post_push(client, after="sha-one")
    assert first.status_code == 200
    assert first.json()["tasks_created"] == 1

    second = await _post_push(client, after="sha-two")
    assert second.status_code == 200
    body = second.json()
    assert body["tasks_created"] == 0
    assert "cooldown" in body["skipped_reason"]


@pytest.mark.anyio
async def test_suppressed_fire_is_recorded_in_history(client: AsyncClient, sdd_dir: Path) -> None:
    """The first push's fire lands in TriggerManager fire history - the
    record the cooldown check on the second push depends on."""
    _write_triggers_config(sdd_dir, cooldown_s=300)

    first = await _post_push(client, after="sha-one")
    assert first.json()["tasks_created"] == 1
    task_id = first.json()["task_ids"][0]

    history = TriggerManager(sdd_dir).get_fire_history()
    assert len(history) == 1
    assert history[0]["trigger_name"] == "push-cooldown"
    assert history[0]["source"] == "github_push"
    assert history[0]["task_id"] == task_id

    # The suppressed second push does not add a second fire record.
    await _post_push(client, after="sha-two")
    assert len(TriggerManager(sdd_dir).get_fire_history()) == 1


@pytest.mark.anyio
async def test_dedup_rule_applies_to_github_push_route(client: AsyncClient, sdd_dir: Path) -> None:
    """A redelivered push (identical branch+sha - the GitHub redelivery case)
    is deduplicated rather than creating a second task."""
    _write_triggers_config(sdd_dir, cooldown_s=0)

    first = await _post_push(client, after="same-sha")
    assert first.json()["tasks_created"] == 1

    replay = await _post_push(client, after="same-sha")
    body = replay.json()
    assert body["tasks_created"] == 0
    assert body["skipped_reason"] == "trigger_suppressed (push-cooldown: deduplicated)"


@pytest.mark.anyio
async def test_routes_without_rules_config_are_behavior_identical(client: AsyncClient, sdd_dir: Path) -> None:
    """AC2: with no triggers.yaml, every push still creates its task -
    the regression the existing route tests already cover, repeated back
    to back to prove there is no hidden cooldown/dedup gate by default."""
    assert not (sdd_dir / "config" / "triggers.yaml").exists()

    first = await _post_push(client, after="sha-one")
    assert first.status_code == 200
    assert first.json()["tasks_created"] == 1

    second = await _post_push(client, after="sha-two")
    assert second.status_code == 200
    assert second.json()["tasks_created"] == 1

    # Even an identical redelivery is unaffected without a trigger rule.
    replay = await _post_push(client, after="sha-one")
    assert replay.json()["tasks_created"] == 1


@pytest.mark.anyio
async def test_push_on_unfiltered_branch_is_not_governed(client: AsyncClient, sdd_dir: Path) -> None:
    """A trigger scoped to `branches: [main]` does not gate pushes to other
    branches - `no_filter_match` is not a suppression of the direct task."""
    _write_triggers_config(sdd_dir, cooldown_s=300, branch="main")

    first = await _post_push(client, ref="refs/heads/feature-x", after="sha-one")
    assert first.json()["tasks_created"] == 1
    second = await _post_push(client, ref="refs/heads/feature-x", after="sha-two")
    assert second.json()["tasks_created"] == 1
