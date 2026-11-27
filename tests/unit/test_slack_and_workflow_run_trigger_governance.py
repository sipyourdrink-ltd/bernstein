"""Slack Events API and GitHub workflow_run routes are governed by triggers.yaml (#4545).

These tests drive the real FastAPI endpoints over ASGI with signed webhook
fixtures, a real ``TaskStore``, and a real ``TriggerManager`` rather than
calling ``TriggerManager.evaluate()`` directly.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, patch

import pytest
import yaml
from httpx import ASGITransport, AsyncClient

from bernstein.core.orchestration.trigger_manager import TriggerManager
from bernstein.core.server import create_app

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

_SLACK_SECRET = "slack-governance-fixture-secret"
_GITHUB_SECRET = "github-workflow-governance-secret"

_RUFF_LOG = (
    "E501 Line too long (130 > 120)\n"
    "  --> src/bernstein/core/foo.py:12:1\n"
    "Found 3 errors.\n"
    "##[error]Process completed with exit code 1.\n"
)


def _slack_headers(body: bytes, secret: str = _SLACK_SECRET) -> dict[str, str]:
    timestamp = str(int(time.time()))
    sig_basestring = f"v0:{timestamp}:{body.decode('utf-8')}"
    signature = "v0=" + hmac.new(secret.encode("utf-8"), sig_basestring.encode("utf-8"), hashlib.sha256).hexdigest()
    return {
        "content-type": "application/json",
        "x-slack-request-timestamp": timestamp,
        "x-slack-signature": signature,
    }


def _slack_message_body(
    *,
    channel: str = "C456DEF",
    user: str = "U123ABC",
    text: str = "investigate payment timeout",
    ts: str = "1700000001.000100",
    team_id: str = "T999XYZ",
) -> bytes:
    payload: dict[str, Any] = {
        "type": "event_callback",
        "team_id": team_id,
        "event": {
            "type": "message",
            "channel": channel,
            "user": user,
            "text": text,
            "ts": ts,
        },
    }
    return json.dumps(payload).encode("utf-8")


def _gh_sign(body: bytes, secret: str = _GITHUB_SECRET) -> str:
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def _workflow_run_body(
    *,
    workflow_name: str = "CI",
    conclusion: str = "failure",
    head_branch: str = "main",
    head_sha: str = "abcdef1234567890",
    run_id: int = 1001,
    sender: str = "alice",
) -> bytes:
    payload: dict[str, Any] = {
        "action": "completed",
        "repository": {"full_name": "acme/widgets"},
        "sender": {"login": sender},
        "workflow_run": {
            "id": run_id,
            "name": workflow_name,
            "conclusion": conclusion,
            "head_branch": head_branch,
            "head_sha": head_sha,
            "html_url": f"https://github.com/acme/widgets/actions/runs/{run_id}",
        },
    }
    return json.dumps(payload).encode("utf-8")


def _write_triggers(sdd_dir: Path, payload: dict[str, Any]) -> Path:
    config_path = sdd_dir / "config" / "triggers.yaml"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(yaml.safe_dump(payload), encoding="utf-8")
    return config_path


@pytest.fixture()
def sdd_dir(tmp_path: Path) -> Path:
    return tmp_path / ".sdd"


@pytest.fixture()
def app(sdd_dir: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", _GITHUB_SECRET)
    monkeypatch.setenv("SLACK_SIGNING_SECRET", _SLACK_SECRET)
    monkeypatch.delenv("SLACK_BOT_USER_ID", raising=False)
    jsonl_path = sdd_dir / "tasks" / "tasks.jsonl"
    jsonl_path.parent.mkdir(parents=True, exist_ok=True)
    return create_app(jsonl_path=jsonl_path, slack_signing_secret=_SLACK_SECRET)


@pytest.fixture()
async def client(app: Any) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def _post_slack(client: AsyncClient, **kwargs: Any) -> Any:
    body = _slack_message_body(**kwargs)
    return await client.post("/webhooks/slack/events", content=body, headers=_slack_headers(body))


async def _post_workflow_run(client: AsyncClient, **kwargs: Any) -> Any:
    body = _workflow_run_body(**kwargs)
    with (
        patch("bernstein.adapters.ci.github_actions.download_github_actions_log", return_value=_RUFF_LOG),
        patch("bernstein.github_app.ci_router.get_commit_files", return_value=[]),
        patch("bernstein.github_app.ci_router.get_commit_diff", return_value=""),
        patch("bernstein.github_app.ci_router.get_commit_message", return_value=""),
    ):
        return await client.post(
            "/webhooks/github",
            content=body,
            headers={
                "X-GitHub-Event": "workflow_run",
                "X-Hub-Signature-256": _gh_sign(body),
            },
        )


@pytest.mark.anyio
async def test_slack_cooldown_suppresses_repeated_event_and_records_fire_history(
    client: AsyncClient, sdd_dir: Path
) -> None:
    """A matching Slack cooldown rule suppresses repeats and records the allowed fire under source='slack'."""
    _write_triggers(
        sdd_dir,
        {
            "triggers": [
                {
                    "name": "slack-ops-cooldown",
                    "source": "slack",
                    "enabled": True,
                    "filters": {"channels": ["C456DEF"]},
                    "conditions": {"cooldown_s": 300},
                }
            ]
        },
    )

    first = await _post_slack(client, channel="C456DEF", ts="1700000001.000100", text="first alert")
    assert first.status_code == 200
    assert first.json() == {"ok": True}

    tasks = (await client.get("/tasks?status=open")).json()
    assert len(tasks) == 1
    created_task_id = tasks[0]["id"]

    history = TriggerManager(sdd_dir).get_fire_history()
    assert len(history) == 1
    assert history[0]["trigger_name"] == "slack-ops-cooldown"
    assert history[0]["source"] == "slack"
    assert history[0]["task_id"] == created_task_id
    assert history[0]["produced"] is True

    second = await _post_slack(client, channel="C456DEF", ts="1700000002.000200", text="second alert")
    assert second.status_code == 200
    assert "cooldown" in second.json().get("skipped_reason", "")
    assert len((await client.get("/tasks?status=open")).json()) == 1
    assert len(TriggerManager(sdd_dir).get_fire_history()) == 1

    # Events in a channel outside the rule's channel filter still pass through unchanged.
    other = await _post_slack(client, channel="C999OTHER", ts="1700000003.000300", text="other channel")
    assert other.status_code == 200
    assert other.json() == {"ok": True}
    assert len((await client.get("/tasks?status=open")).json()) == 2
    assert len(TriggerManager(sdd_dir).get_fire_history()) == 1


@pytest.mark.anyio
async def test_slack_passthrough_without_rules_matches_baseline_before_enabling_policy(
    client: AsyncClient, sdd_dir: Path
) -> None:
    """Missing triggers.yaml or non-matching rules keep Slack task creation and response byte-identical."""
    assert not (sdd_dir / "config" / "triggers.yaml").exists()

    first = await _post_slack(client, channel="C456DEF", ts="1700000001.000100", text="baseline one")
    second = await _post_slack(client, channel="C456DEF", ts="1700000002.000200", text="baseline two")
    assert first.status_code == 200 and first.json() == {"ok": True}
    assert second.status_code == 200 and second.json() == {"ok": True}
    assert not (sdd_dir / "runtime" / "triggers").exists()

    baseline_tasks = (await client.get("/tasks?status=open")).json()
    assert len(baseline_tasks) == 2
    assert baseline_tasks[0]["title"] == "baseline one"
    assert baseline_tasks[0]["role"] == "backend"
    assert baseline_tasks[0]["slack_context"] == {
        "channel": "C456DEF",
        "user": "U123ABC",
        "thread_ts": "1700000001.000100",
    }

    # A triggers.yaml with no slack rule remains a transparent pass-through.
    _write_triggers(
        sdd_dir,
        {
            "triggers": [
                {
                    "name": "push-only",
                    "source": "github_push",
                    "enabled": True,
                    "filters": {"branches": ["main"]},
                    "conditions": {"cooldown_s": 300},
                }
            ]
        },
    )
    unmatched = await _post_slack(client, channel="C456DEF", ts="1700000003.000300", text="unmatched rule")
    assert unmatched.status_code == 200 and unmatched.json() == {"ok": True}
    assert len((await client.get("/tasks?status=open")).json()) == 3
    assert TriggerManager(sdd_dir).get_fire_history() == []

    # Enabling a matching slack rule keeps the allowed response/task shape identical and suppresses repeats.
    _write_triggers(
        sdd_dir,
        {
            "triggers": [
                {
                    "name": "slack-cooldown",
                    "source": "slack",
                    "enabled": True,
                    "filters": {"channels": ["C456DEF"]},
                    "conditions": {"cooldown_s": 300},
                }
            ]
        },
    )
    governed = await _post_slack(client, channel="C456DEF", ts="1700000004.000400", text="governed one")
    assert governed.status_code == 200 and governed.json() == {"ok": True}
    suppressed = await _post_slack(client, channel="C456DEF", ts="1700000005.000500", text="governed two")
    assert suppressed.status_code == 200
    assert "cooldown" in suppressed.json().get("skipped_reason", "")
    assert len((await client.get("/tasks?status=open")).json()) == 4


@pytest.mark.anyio
async def test_slack_record_fire_runs_only_after_successful_store_create(
    app: Any, client: AsyncClient, sdd_dir: Path
) -> None:
    """A failed store.create does not append a fire-history record; a subsequent allowed create does."""
    _write_triggers(
        sdd_dir,
        {
            "triggers": [
                {
                    "name": "slack-create-guard",
                    "source": "slack",
                    "enabled": True,
                    "filters": {"channels": ["C456DEF", "C789GHI"]},
                    "conditions": {"cooldown_s": 300},
                }
            ]
        },
    )

    with patch.object(app.state.store, "create", new=AsyncMock(side_effect=RuntimeError("store unavailable"))):
        failed = await _post_slack(client, channel="C456DEF", ts="1700000001.000100", text="fails to persist")
        assert failed.status_code == 500

    assert TriggerManager(sdd_dir).get_fire_history() == []

    # Because no fire was recorded, cooldown did not arm; an unreserved channel succeeds and records fire.
    recovered = await _post_slack(client, channel="C789GHI", ts="1700000002.000200", text="persisted task")
    assert recovered.status_code == 200
    assert recovered.json() == {"ok": True}
    history = TriggerManager(sdd_dir).get_fire_history()
    assert len(history) == 1
    assert history[0]["source"] == "slack"
    assert history[0]["trigger_name"] == "slack-create-guard"


@pytest.mark.anyio
async def test_workflow_run_filters_by_workflow_name_applies_cooldown_and_records_source(
    client: AsyncClient, sdd_dir: Path
) -> None:
    """github_workflow_run rules filter by workflow_names, enforce cooldown, and record fire under github_workflow_run."""
    assert not (sdd_dir / "config" / "triggers.yaml").exists()

    # Without triggers.yaml, workflow_run failures pass through without creating trigger state.
    baseline = await _post_workflow_run(client, workflow_name="CI", head_branch="baseline", head_sha="base0001")
    assert baseline.status_code == 200
    assert baseline.json()["tasks_created"] == 1
    assert "skipped_reason" not in baseline.json()
    assert not (sdd_dir / "runtime" / "triggers").exists()

    _write_triggers(
        sdd_dir,
        {
            "triggers": [
                {
                    "name": "ci-workflow-cooldown",
                    "source": "github_workflow_run",
                    "enabled": True,
                    "filters": {"conclusion": "failure", "workflow_names": ["CI"]},
                    "conditions": {"cooldown_s": 300},
                }
            ]
        },
    )

    first = await _post_workflow_run(client, workflow_name="CI", head_branch="main", head_sha="sha-one-1111")
    assert first.status_code == 200
    first_data = first.json()
    assert first_data["tasks_created"] == 1
    task_id = first_data["task_ids"][0]

    history = TriggerManager(sdd_dir).get_fire_history()
    assert len(history) == 1
    assert history[0]["trigger_name"] == "ci-workflow-cooldown"
    assert history[0]["source"] == "github_workflow_run"
    assert history[0]["task_id"] == task_id

    second = await _post_workflow_run(client, workflow_name="CI", head_branch="main", head_sha="sha-two-2222")
    assert second.status_code == 200
    second_data = second.json()
    assert second_data["tasks_created"] == 0
    assert second_data["task_ids"] == []
    assert "cooldown" in second_data["skipped_reason"]
    assert len(TriggerManager(sdd_dir).get_fire_history()) == 1

    # A workflow whose name does not match workflow_names passes through untouched.
    unmatched = await _post_workflow_run(client, workflow_name="Lint", head_branch="other", head_sha="sha-three-3333")
    assert unmatched.status_code == 200
    assert unmatched.json()["tasks_created"] == 1
    assert "skipped_reason" not in unmatched.json()
    assert len(TriggerManager(sdd_dir).get_fire_history()) == 1


@pytest.mark.anyio
async def test_workflow_run_dedup_suppresses_redelivered_failure_under_github_workflow_run(
    client: AsyncClient, sdd_dir: Path
) -> None:
    """A redelivered workflow_run failure (same branch+sha) is deduplicated under source='github_workflow_run'."""
    _write_triggers(
        sdd_dir,
        {
            "triggers": [
                {
                    "name": "ci-workflow-dedup",
                    "source": "github_workflow_run",
                    "enabled": True,
                    "filters": {"conclusion": "failure", "workflow_names": ["CI"]},
                    "conditions": {"cooldown_s": 0},
                }
            ]
        },
    )

    first = await _post_workflow_run(client, workflow_name="CI", head_branch="main", head_sha="same-sha-1234")
    assert first.status_code == 200
    assert first.json()["tasks_created"] == 1

    replay = await _post_workflow_run(client, workflow_name="CI", head_branch="main", head_sha="same-sha-1234")
    assert replay.status_code == 200
    assert replay.json()["tasks_created"] == 0
    assert replay.json()["skipped_reason"] == "trigger_suppressed (ci-workflow-dedup: deduplicated)"

    history = TriggerManager(sdd_dir).get_fire_history()
    assert len(history) == 1
    assert history[0]["source"] == "github_workflow_run"


@pytest.mark.anyio
@pytest.mark.parametrize(
    "corrupt_mode",
    ["malformed_yaml", "wrong_root_shape", "permission_error"],
)
async def test_corrupt_or_unreadable_triggers_yaml_fails_closed_on_slack(
    client: AsyncClient, sdd_dir: Path, monkeypatch: pytest.MonkeyPatch, corrupt_mode: str
) -> None:
    """Corrupt or unreadable triggers.yaml suppresses Slack task creation instead of failing open."""
    config_path = sdd_dir / "config" / "triggers.yaml"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    if corrupt_mode == "malformed_yaml":
        config_path.write_text("not: [valid yaml\n", encoding="utf-8")
    elif corrupt_mode == "wrong_root_shape":
        config_path.write_text("triggers: {}\n", encoding="utf-8")
    else:
        config_path.write_text("triggers: [{name: slack-rule, source: slack}]\n", encoding="utf-8")

    real_stat = type(config_path).stat

    def denied_stat(target: Path, *args: Any, **kwargs: Any) -> Any:
        if target == config_path:
            raise PermissionError("trigger policy denied")
        return real_stat(target, *args, **kwargs)

    with monkeypatch.context() as mp:
        if corrupt_mode == "permission_error":
            mp.setattr(type(config_path), "stat", denied_stat)
        resp = await _post_slack(client, channel="C456DEF", ts="1700000001.000100", text="must not bypass")

    assert resp.status_code == 200
    assert "trigger policy unavailable" in resp.json().get("skipped_reason", "")
    assert (await client.get("/tasks?status=open")).json() == []


@pytest.mark.anyio
@pytest.mark.parametrize(
    "corrupt_mode",
    ["malformed_yaml", "wrong_root_shape", "permission_error"],
)
async def test_corrupt_or_unreadable_triggers_yaml_fails_closed_on_workflow_run(
    client: AsyncClient, sdd_dir: Path, monkeypatch: pytest.MonkeyPatch, corrupt_mode: str
) -> None:
    """Corrupt or unreadable triggers.yaml suppresses workflow_run task creation instead of failing open."""
    config_path = sdd_dir / "config" / "triggers.yaml"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    if corrupt_mode == "malformed_yaml":
        config_path.write_text("not: [valid yaml\n", encoding="utf-8")
    elif corrupt_mode == "wrong_root_shape":
        config_path.write_text("triggers: {}\n", encoding="utf-8")
    else:
        config_path.write_text("triggers: [{name: ci-rule, source: github_workflow_run}]\n", encoding="utf-8")

    real_stat = type(config_path).stat

    def denied_stat(target: Path, *args: Any, **kwargs: Any) -> Any:
        if target == config_path:
            raise PermissionError("trigger policy denied")
        return real_stat(target, *args, **kwargs)

    with monkeypatch.context() as mp:
        if corrupt_mode == "permission_error":
            mp.setattr(type(config_path), "stat", denied_stat)
        resp = await _post_workflow_run(client, workflow_name="CI", head_branch="main", head_sha="corrupt-sha")

    assert resp.status_code == 200
    data = resp.json()
    assert data["tasks_created"] == 0
    assert data["task_ids"] == []
    assert "trigger policy unavailable" in data.get("skipped_reason", "")
    assert (await client.get("/tasks?status=open")).json() == []
