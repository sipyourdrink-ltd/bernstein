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

from bernstein.core.orchestration.trigger_manager import TriggerManager, compute_dedup_key
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


def _slack_headers(body: bytes, secret: str = _SLACK_SECRET, extra: dict[str, str] | None = None) -> dict[str, str]:
    timestamp = str(int(time.time()))
    sig_basestring = f"v0:{timestamp}:{body.decode('utf-8')}"
    signature = "v0=" + hmac.new(secret.encode("utf-8"), sig_basestring.encode("utf-8"), hashlib.sha256).hexdigest()
    headers = {
        "content-type": "application/json",
        "x-slack-request-timestamp": timestamp,
        "x-slack-signature": signature,
    }
    if extra:
        headers.update(extra)
    return headers


def _slack_message_body(
    *,
    channel: str = "C456DEF",
    user: str = "U123ABC",
    text: str = "investigate payment timeout",
    ts: str = "1700000001.000100",
    team_id: str = "T999XYZ",
    event_id: str | None = None,
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
    if event_id is not None:
        payload["event_id"] = event_id
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
    run_attempt: int | None = None,
    sender: str = "alice",
) -> bytes:
    workflow_run: dict[str, Any] = {
        "id": run_id,
        "name": workflow_name,
        "conclusion": conclusion,
        "head_branch": head_branch,
        "head_sha": head_sha,
        "html_url": f"https://github.com/acme/widgets/actions/runs/{run_id}",
    }
    if run_attempt is not None:
        workflow_run["run_attempt"] = run_attempt
    payload: dict[str, Any] = {
        "action": "completed",
        "repository": {"full_name": "acme/widgets"},
        "sender": {"login": sender},
        "workflow_run": workflow_run,
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


async def _post_slack(client: AsyncClient, *, extra_headers: dict[str, str] | None = None, **kwargs: Any) -> Any:
    body = _slack_message_body(**kwargs)
    return await client.post("/webhooks/slack/events", content=body, headers=_slack_headers(body, extra=extra_headers))


async def _post_workflow_run(client: AsyncClient, *, extra_headers: dict[str, str] | None = None, **kwargs: Any) -> Any:
    body = _workflow_run_body(**kwargs)
    headers = {
        "X-GitHub-Event": "workflow_run",
        "X-Hub-Signature-256": _gh_sign(body),
    }
    if extra_headers:
        headers.update(extra_headers)
    with (
        patch("bernstein.adapters.ci.github_actions.download_github_actions_log", return_value=_RUFF_LOG),
        patch("bernstein.github_app.ci_router.get_commit_files", return_value=[]),
        patch("bernstein.github_app.ci_router.get_commit_diff", return_value=""),
        patch("bernstein.github_app.ci_router.get_commit_message", return_value=""),
    ):
        return await client.post("/webhooks/github", content=body, headers=headers)


@pytest.mark.anyio
async def test_slack_cooldown_suppresses_repeated_event_and_records_fire_history(
    client: AsyncClient, sdd_dir: Path
) -> None:
    """A matching Slack cooldown rule suppresses repeats and records both allowed and suppressed fires."""
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
    history = TriggerManager(sdd_dir).get_fire_history()
    assert len(history) == 2
    assert history[1]["source"] == "slack"
    assert history[1]["produced"] is False

    # Events in a channel outside the rule's channel filter still pass through unchanged.
    other = await _post_slack(client, channel="C999OTHER", ts="1700000003.000300", text="other channel")
    assert other.status_code == 200
    assert other.json() == {"ok": True}
    assert len((await client.get("/tasks?status=open")).json()) == 2
    assert len(TriggerManager(sdd_dir).get_fire_history()) == 2


@pytest.mark.anyio
async def test_slack_dedup_allows_distinct_messages_in_same_channel_and_deduplicates_redelivery(
    client: AsyncClient, sdd_dir: Path
) -> None:
    """Distinct Slack messages in one channel succeed under cooldown_s=0 while exact redeliveries deduplicate."""
    _write_triggers(
        sdd_dir,
        {
            "triggers": [
                {
                    "name": "slack-filter-no-cooldown",
                    "source": "slack",
                    "enabled": True,
                    "filters": {"channels": ["C111"]},
                    "conditions": {"cooldown_s": 0},
                }
            ]
        },
    )

    first = await _post_slack(client, channel="C111", user="U1", ts="1700000001.000100", text="first distinct msg")
    assert first.status_code == 200
    assert first.json() == {"ok": True}

    second = await _post_slack(
        client, channel="C111", user="U2", ts="1700000099.000999", text="completely different second msg"
    )
    assert second.status_code == 200
    assert second.json() == {"ok": True}
    assert len((await client.get("/tasks?status=open")).json()) == 2

    # Redelivering the exact same (channel, ts) message is deduplicated.
    replay_ts = await _post_slack(client, channel="C111", user="U1", ts="1700000001.000100", text="first distinct msg")
    assert replay_ts.status_code == 200
    assert "deduplicated" in replay_ts.json().get("skipped_reason", "")
    assert len((await client.get("/tasks?status=open")).json()) == 2

    # event_id distinguishes deliveries and deduplicates retries of the same event_id.
    ev1 = await _post_slack(
        client, channel="C111", user="U3", ts="1700000200.000100", event_id="Ev001", text="event one"
    )
    assert ev1.status_code == 200
    assert ev1.json() == {"ok": True}

    ev1_replay = await _post_slack(
        client, channel="C111", user="U3", ts="1700000200.000100", event_id="Ev001", text="event one"
    )
    assert ev1_replay.status_code == 200
    assert "deduplicated" in ev1_replay.json().get("skipped_reason", "")

    ev2 = await _post_slack(
        client, channel="C111", user="U3", ts="1700000200.000100", event_id="Ev002", text="event two"
    )
    assert ev2.status_code == 200
    assert ev2.json() == {"ok": True}
    assert len((await client.get("/tasks?status=open")).json()) == 4


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
async def test_slack_retry_after_failed_store_create_succeeds_and_unconfirmed_reservation_returns_503(
    app: Any, client: AsyncClient, sdd_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Failed store.create releases its reservation for retry; unconfirmed reservations return 503."""
    _write_triggers(
        sdd_dir,
        {
            "triggers": [
                {
                    "name": "slack-create-guard",
                    "source": "slack",
                    "enabled": True,
                    "filters": {"channels": ["C456DEF"]},
                    "conditions": {"cooldown_s": 0},
                }
            ]
        },
    )

    with patch.object(app.state.store, "create", new=AsyncMock(side_effect=RuntimeError("store unavailable"))):
        failed = await _post_slack(
            client,
            channel="C456DEF",
            ts="1700000001.000100",
            event_id="EvRetry1",
            text="fails to persist",
        )
        assert failed.status_code == 500

    assert TriggerManager(sdd_dir).get_fire_history() == []

    # Slack retries the exact same signed delivery once store recovers -> task is created, not dropped.
    retried = await _post_slack(
        client,
        channel="C456DEF",
        ts="1700000001.000100",
        event_id="EvRetry1",
        text="fails to persist",
    )
    assert retried.status_code == 200
    assert retried.json() == {"ok": True}
    tasks = (await client.get("/tasks?status=open")).json()
    assert len(tasks) == 1
    history = TriggerManager(sdd_dir).get_fire_history()
    assert len(history) == 1
    assert history[0]["source"] == "slack"
    assert history[0]["trigger_name"] == "slack-create-guard"
    assert history[0]["produced"] is True

    # A redelivery after confirmed persistence is deduplicated with 200 OK.
    confirmed_dup = await _post_slack(
        client,
        channel="C456DEF",
        ts="1700000001.000100",
        event_id="EvRetry1",
        text="fails to persist",
    )
    assert confirmed_dup.status_code == 200
    assert "deduplicated" in confirmed_dup.json().get("skipped_reason", "")
    assert len((await client.get("/tasks?status=open")).json()) == 1

    # When fire history cannot confirm a reserved delivery (e.g. record_fire failed), retry gets 503.
    def unavailable(*_args: Any, **_kwargs: Any) -> None:
        raise OSError("history unavailable")

    with monkeypatch.context() as mp:
        mp.setattr(TriggerManager, "record_fire", unavailable)
        unlogged = await _post_slack(
            client,
            channel="C456DEF",
            ts="1700000002.000200",
            event_id="EvUnconfirmed1",
            text="history write fails",
        )
        assert unlogged.status_code == 200
        assert unlogged.json() == {"ok": True}

    unconfirmed_retry = await _post_slack(
        client,
        channel="C456DEF",
        ts="1700000002.000200",
        event_id="EvUnconfirmed1",
        text="history write fails",
    )
    assert unconfirmed_retry.status_code == 503
    assert "unconfirmed" in unconfirmed_retry.json().get("detail", "")


@pytest.mark.anyio
async def test_slack_pending_unconfirmed_reservation_returns_503_during_cooldown(
    client: AsyncClient, sdd_dir: Path
) -> None:
    """An unconfirmed dedup reservation still returns 503 even after another message arms cooldown."""
    from bernstein.core.trigger_sources.slack import normalize_slack_message

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
    pending_payload = json.loads(
        _slack_message_body(channel="C456DEF", ts="1700000010.000100", event_id="EvPending1", text="pending").decode()
    )
    pending_event = normalize_slack_message(pending_payload)
    pending_event.metadata["cooldown_scope"] = hashlib.sha256(b"default").hexdigest()
    key = compute_dedup_key("slack-cooldown", pending_event)
    assert TriggerManager(sdd_dir).claim_dedup(key, 300)

    # Another message succeeds and arms the 300s cooldown window.
    armed = await _post_slack(
        client, channel="C456DEF", ts="1700000011.000100", event_id="EvArmed1", text="arms cooldown"
    )
    assert armed.status_code == 200
    assert armed.json() == {"ok": True}

    # Retrying the unconfirmed pending delivery still surfaces 503 rather than 200 cooldown.
    retried_pending = await _post_slack(
        client, channel="C456DEF", ts="1700000010.000100", event_id="EvPending1", text="pending"
    )
    assert retried_pending.status_code == 503
    assert "unconfirmed" in retried_pending.json().get("detail", "")


@pytest.mark.anyio
async def test_slack_mention_required_matches_real_bot_user_mention_and_enforces_cooldown(
    client: AsyncClient, sdd_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """filters.mention_required=true matches real Slack <@U...> mentions instead of failing open."""
    monkeypatch.setenv("SLACK_BOT_USER_ID", "U01BOTUSER")
    _write_triggers(
        sdd_dir,
        {
            "triggers": [
                {
                    "name": "slack-mention-cooldown",
                    "source": "slack",
                    "enabled": True,
                    "filters": {"channels": ["C111"], "mention_required": True},
                    "conditions": {"cooldown_s": 300},
                }
            ]
        },
    )

    first = await _post_slack(
        client, channel="C111", ts="1700000001.000100", text="<@U01BOTUSER> investigate flaky checkout"
    )
    assert first.status_code == 200
    assert first.json() == {"ok": True}

    second = await _post_slack(
        client, channel="C111", ts="1700000002.000200", text="<@U01BOTUSER> second request in window"
    )
    assert second.status_code == 200
    assert "cooldown" in second.json().get("skipped_reason", "")
    assert len((await client.get("/tasks?status=open")).json()) == 1


@pytest.mark.anyio
async def test_slack_malformed_ts_and_empty_mention_do_not_crash(
    client: AsyncClient, sdd_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Malformed ts strings and mention-only messages do not raise 500 on POST /webhooks/slack/events."""
    monkeypatch.setenv("SLACK_BOT_USER_ID", "U01BOTUSER")

    empty_mention = await _post_slack(client, channel="C111", ts="not-a-float", text="<@U01BOTUSER>   ")
    assert empty_mention.status_code == 200
    assert empty_mention.json() == {"ok": True}
    assert (await client.get("/tasks?status=open")).json() == []

    bad_ts = await _post_slack(client, channel="C111", ts="not-a-float", text="<@U01BOTUSER> handle non-numeric ts")
    assert bad_ts.status_code == 200
    assert bad_ts.json() == {"ok": True}
    assert len((await client.get("/tasks?status=open")).json()) == 1


@pytest.mark.anyio
async def test_workflow_run_filters_by_workflow_name_applies_cooldown_and_records_source(
    client: AsyncClient, sdd_dir: Path
) -> None:
    """github_workflow_run rules filter by workflow_names, enforce cooldown, and record fire history."""
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

    first = await _post_workflow_run(
        client, workflow_name="CI", head_branch="main", head_sha="sha-one-1111", run_id=2001
    )
    assert first.status_code == 200
    first_data = first.json()
    assert first_data["tasks_created"] == 1
    task_id = first_data["task_ids"][0]

    history = TriggerManager(sdd_dir).get_fire_history()
    assert len(history) == 1
    assert history[0]["trigger_name"] == "ci-workflow-cooldown"
    assert history[0]["source"] == "github_workflow_run"
    assert history[0]["task_id"] == task_id
    assert history[0]["produced"] is True

    second = await _post_workflow_run(
        client, workflow_name="CI", head_branch="main", head_sha="sha-two-2222", run_id=2002
    )
    assert second.status_code == 200
    second_data = second.json()
    assert second_data["tasks_created"] == 0
    assert second_data["task_ids"] == []
    assert "cooldown" in second_data["skipped_reason"]
    history = TriggerManager(sdd_dir).get_fire_history()
    assert len(history) == 2
    assert history[1]["source"] == "github_workflow_run"
    assert history[1]["produced"] is False

    # A workflow whose name does not match workflow_names passes through untouched.
    unmatched = await _post_workflow_run(
        client, workflow_name="Lint", head_branch="other", head_sha="sha-three-3333", run_id=2003
    )
    assert unmatched.status_code == 200
    assert unmatched.json()["tasks_created"] == 1
    assert "skipped_reason" not in unmatched.json()
    assert len(TriggerManager(sdd_dir).get_fire_history()) == 2


@pytest.mark.anyio
async def test_workflow_run_dedup_distinguishes_workflows_and_reruns_at_same_commit(
    client: AsyncClient, sdd_dir: Path
) -> None:
    """Distinct failing workflows and re-run attempts on the same (branch, sha) are not falsely deduplicated."""
    _write_triggers(
        sdd_dir,
        {
            "triggers": [
                {
                    "name": "all-ci-failures",
                    "source": "github_workflow_run",
                    "enabled": True,
                    "filters": {"conclusion": "failure"},
                    "conditions": {"cooldown_s": 0},
                }
            ]
        },
    )

    first = await _post_workflow_run(
        client,
        workflow_name="Unit Tests",
        head_branch="feat/x",
        head_sha="deadbeef",
        run_id=101,
        run_attempt=1,
    )
    assert first.status_code == 200
    assert first.json()["tasks_created"] == 1

    second_workflow = await _post_workflow_run(
        client,
        workflow_name="CodeQL",
        head_branch="feat/x",
        head_sha="deadbeef",
        run_id=102,
        run_attempt=1,
    )
    assert second_workflow.status_code == 200
    assert second_workflow.json()["tasks_created"] == 1

    rerun_attempt = await _post_workflow_run(
        client,
        workflow_name="Unit Tests",
        head_branch="feat/x",
        head_sha="deadbeef",
        run_id=101,
        run_attempt=2,
    )
    assert rerun_attempt.status_code == 200
    assert rerun_attempt.json()["tasks_created"] == 1

    # Redelivering the exact same (run_id, run_attempt) is deduplicated.
    replay = await _post_workflow_run(
        client,
        workflow_name="Unit Tests",
        head_branch="feat/x",
        head_sha="deadbeef",
        run_id=101,
        run_attempt=1,
    )
    assert replay.status_code == 200
    assert replay.json()["tasks_created"] == 0
    assert replay.json()["skipped_reason"] == "trigger_suppressed (all-ci-failures: deduplicated)"

    history = TriggerManager(sdd_dir).get_fire_history()
    assert len(history) == 4
    assert [row["produced"] for row in history] == [True, True, True, False]
    assert all(row["source"] == "github_workflow_run" for row in history)


@pytest.mark.anyio
async def test_workflow_run_retry_after_failed_store_create_succeeds_and_unconfirmed_reservation_returns_503(
    app: Any, client: AsyncClient, sdd_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Failed store.create releases workflow_run reservation for retry; unconfirmed reservations return 503."""
    _write_triggers(
        sdd_dir,
        {
            "triggers": [
                {
                    "name": "ci-workflow-retry",
                    "source": "github_workflow_run",
                    "enabled": True,
                    "filters": {"conclusion": "failure", "workflow_names": ["CI"]},
                    "conditions": {"cooldown_s": 0},
                }
            ]
        },
    )

    with patch.object(app.state.store, "create", new=AsyncMock(side_effect=RuntimeError("store unavailable"))):
        failed = await _post_workflow_run(
            client, workflow_name="CI", head_branch="main", head_sha="retry-sha", run_id=3001, run_attempt=1
        )
        assert failed.status_code == 500

    assert TriggerManager(sdd_dir).get_fire_history() == []

    retried = await _post_workflow_run(
        client, workflow_name="CI", head_branch="main", head_sha="retry-sha", run_id=3001, run_attempt=1
    )
    assert retried.status_code == 200
    assert retried.json()["tasks_created"] == 1

    # Unconfirmed delivery after record_fire OSError returns 503 on redelivery.
    def unavailable(*_args: Any, **_kwargs: Any) -> None:
        raise OSError("history unavailable")

    with monkeypatch.context() as mp:
        mp.setattr(TriggerManager, "record_fire", unavailable)
        unlogged = await _post_workflow_run(
            client, workflow_name="CI", head_branch="main", head_sha="retry-sha", run_id=3002, run_attempt=1
        )
        assert unlogged.status_code == 200
        assert unlogged.json()["tasks_created"] == 1

    unconfirmed_retry = await _post_workflow_run(
        client, workflow_name="CI", head_branch="main", head_sha="retry-sha", run_id=3002, run_attempt=1
    )
    assert unconfirmed_retry.status_code == 503
    assert "unconfirmed" in unconfirmed_retry.json().get("detail", "")


@pytest.mark.anyio
async def test_slack_and_workflow_run_cooldown_and_dedup_are_tenant_scoped(
    client: AsyncClient, sdd_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Tenant A's Slack or workflow_run delivery does not suppress Tenant B's delivery."""
    _write_triggers(
        sdd_dir,
        {
            "triggers": [
                {
                    "name": "slack-tenant-cooldown",
                    "source": "slack",
                    "enabled": True,
                    "filters": {"channels": ["C456DEF"]},
                    "conditions": {"cooldown_s": 300},
                },
                {
                    "name": "wf-tenant-cooldown",
                    "source": "github_workflow_run",
                    "enabled": True,
                    "filters": {"conclusion": "failure", "workflow_names": ["CI"]},
                    "conditions": {"cooldown_s": 300},
                },
            ]
        },
    )

    def _fixture_tenant(request: Any) -> str:
        return str(request.headers["X-Fixture-Tenant"])

    monkeypatch.setattr("bernstein.core.routes.slack.request_tenant_id", _fixture_tenant)
    monkeypatch.setattr("bernstein.core.routes.webhooks.request_tenant_id", _fixture_tenant)

    for tenant in ("tenant-a", "tenant-b"):
        slack_resp = await _post_slack(
            client,
            channel="C456DEF",
            ts="1700000001.000100",
            event_id="EvShared",
            text="same payload per tenant",
            extra_headers={"X-Fixture-Tenant": tenant},
        )
        assert slack_resp.status_code == 200
        assert slack_resp.json() == {"ok": True}

        wf_resp = await _post_workflow_run(
            client,
            workflow_name="CI",
            head_branch="main",
            head_sha="same-sha",
            run_id=5001,
            run_attempt=1,
            extra_headers={"X-Fixture-Tenant": tenant},
        )
        assert wf_resp.status_code == 200
        assert wf_resp.json()["tasks_created"] == 1

    # Tenant A's follow-up deliveries are suppressed within Tenant A's cooldown window.
    blocked_slack = await _post_slack(
        client,
        channel="C456DEF",
        ts="1700000002.000200",
        event_id="EvTenantA2",
        text="second tenant-a message",
        extra_headers={"X-Fixture-Tenant": "tenant-a"},
    )
    assert blocked_slack.status_code == 200
    assert "cooldown" in blocked_slack.json().get("skipped_reason", "")

    blocked_wf = await _post_workflow_run(
        client,
        workflow_name="CI",
        head_branch="main",
        head_sha="sha-a2",
        run_id=5002,
        run_attempt=1,
        extra_headers={"X-Fixture-Tenant": "tenant-a"},
    )
    assert blocked_wf.status_code == 200
    assert blocked_wf.json()["tasks_created"] == 0
    assert "cooldown" in blocked_wf.json().get("skipped_reason", "")


@pytest.mark.anyio
@pytest.mark.parametrize(
    "corrupt_mode",
    ["malformed_yaml", "wrong_root_shape", "permission_error", "quarantined"],
)
async def test_corrupt_or_unreadable_triggers_yaml_fails_closed_on_slack(
    client: AsyncClient, sdd_dir: Path, monkeypatch: pytest.MonkeyPatch, corrupt_mode: str
) -> None:
    """Corrupt, unreadable, or quarantined triggers.yaml returns 503 on event_callback while url_verification stays 200."""
    config_path = sdd_dir / "config" / "triggers.yaml"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    if corrupt_mode == "malformed_yaml":
        config_path.write_text("not: [valid yaml\n", encoding="utf-8")
    elif corrupt_mode == "wrong_root_shape":
        config_path.write_text("triggers: {}\n", encoding="utf-8")
    elif corrupt_mode == "quarantined":
        config_path.write_text(
            yaml.safe_dump(
                {"triggers": [{"name": "bad-slack-rule", "source": "slack", "task": {"priority": "invalid"}}]}
            ),
            encoding="utf-8",
        )
    else:
        config_path.write_text("triggers: [{name: slack-rule, source: slack}]\n", encoding="utf-8")

    real_stat = type(config_path).stat

    def denied_stat(target: Path, *args: Any, **kwargs: Any) -> Any:
        if target == config_path:
            raise PermissionError("trigger policy denied")
        return real_stat(target, *args, **kwargs)

    challenge_body = json.dumps({"type": "url_verification", "challenge": "abc-challenge"}).encode("utf-8")

    with monkeypatch.context() as mp:
        if corrupt_mode == "permission_error":
            mp.setattr(type(config_path), "stat", denied_stat)
        verify_resp = await client.post(
            "/webhooks/slack/events", content=challenge_body, headers=_slack_headers(challenge_body)
        )
        resp = await _post_slack(client, channel="C456DEF", ts="1700000001.000100", text="must not bypass")

    assert verify_resp.status_code == 200
    assert verify_resp.json() == {"challenge": "abc-challenge"}
    assert resp.status_code == 503
    expected_reason = "declaration quarantined" if corrupt_mode == "quarantined" else "trigger policy unavailable"
    assert expected_reason in resp.json().get("detail", "")
    assert (await client.get("/tasks?status=open")).json() == []


@pytest.mark.anyio
@pytest.mark.parametrize(
    "corrupt_mode",
    ["malformed_yaml", "wrong_root_shape", "permission_error", "quarantined"],
)
async def test_corrupt_or_unreadable_triggers_yaml_fails_closed_on_workflow_run(
    client: AsyncClient, sdd_dir: Path, monkeypatch: pytest.MonkeyPatch, corrupt_mode: str
) -> None:
    """Corrupt, unreadable, or quarantined triggers.yaml returns 503 on workflow_run instead of failing open."""
    config_path = sdd_dir / "config" / "triggers.yaml"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    if corrupt_mode == "malformed_yaml":
        config_path.write_text("not: [valid yaml\n", encoding="utf-8")
    elif corrupt_mode == "wrong_root_shape":
        config_path.write_text("triggers: {}\n", encoding="utf-8")
    elif corrupt_mode == "quarantined":
        config_path.write_text(
            yaml.safe_dump(
                {
                    "triggers": [
                        {"name": "bad-ci-rule", "source": "github_workflow_run", "task": {"priority": "invalid"}}
                    ]
                }
            ),
            encoding="utf-8",
        )
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

    assert resp.status_code == 503
    expected_reason = "declaration quarantined" if corrupt_mode == "quarantined" else "trigger policy unavailable"
    assert expected_reason in resp.json().get("detail", "")
    assert (await client.get("/tasks?status=open")).json() == []
