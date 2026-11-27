"""Exercise GitLab trigger policies through the task-creating HTTP route."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
import yaml
from httpx import ASGITransport, AsyncClient

from bernstein.core.orchestration.trigger_manager import TriggerManager
from bernstein.core.server import create_app

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path


@pytest.fixture()
async def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[AsyncClient]:
    monkeypatch.setenv("GITLAB_WEBHOOK_TOKEN", "gitlab-fixture-token")
    jsonl_path = tmp_path / ".sdd" / "tasks" / "tasks.jsonl"
    jsonl_path.parent.mkdir(parents=True)
    app = create_app(jsonl_path=jsonl_path)
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"X-Gitlab-Token": "gitlab-fixture-token"},
    ) as client:
        yield client


def _configure(tmp_path: Path, *, cooldown: int) -> Path:
    sdd_dir = tmp_path / ".sdd"
    path = sdd_dir / "config" / "triggers.yaml"
    path.parent.mkdir(parents=True)
    path.write_text(
        yaml.safe_dump(
            {
                "triggers": [
                    {
                        "name": "release-pipeline",
                        "source": "gitlab_webhook",
                        "filters": {"events": ["pipeline"], "branches": ["release/*"]},
                        "conditions": {"cooldown_s": cooldown},
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    return sdd_dir


def _pipeline(number: int, *, branch: str = "release/stable") -> dict[str, Any]:
    return {
        "object_kind": "pipeline",
        "object_attributes": {"id": number, "ref": branch, "status": "failed", "sha": "abcdef012345"},
        "project": {"id": 12, "path_with_namespace": "test/widget"},
        "user": {"username": "tester"},
    }


@pytest.mark.anyio
async def test_matching_gitlab_rule_applies_cooldown_and_records_history(client: AsyncClient, tmp_path: Path) -> None:
    sdd_dir = _configure(tmp_path, cooldown=300)
    first = await client.post("/webhooks/gitlab", json=_pipeline(1))
    assert first.status_code == 200, first.text
    assert first.json()["tasks_created"] == 1

    second = await client.post("/webhooks/gitlab", json=_pipeline(2))
    assert second.status_code == 200, second.text
    assert second.json()["tasks_created"] == 0
    assert "cooldown" in second.json()["skipped_reason"]

    other_branch = await client.post("/webhooks/gitlab", json=_pipeline(3, branch="feature/widget"))
    assert other_branch.status_code == 200, other_branch.text
    assert other_branch.json()["tasks_created"] == 1

    history = TriggerManager(sdd_dir).get_fire_history()
    assert len(history) == 2
    assert history[0]["task_id"] == first.json()["task_ids"][0]
    assert history[0]["produced"] is True
    assert history[1]["produced"] is False
    assert history[1]["source"] == "gitlab_webhook"


@pytest.mark.anyio
async def test_gitlab_dedup_distinguishes_pipeline_runs_at_same_commit(client: AsyncClient, tmp_path: Path) -> None:
    _configure(tmp_path, cooldown=0)
    for number in (1, 2):
        response = await client.post("/webhooks/gitlab", json=_pipeline(number))
        assert response.status_code == 200, response.text
        assert response.json()["tasks_created"] == 1

    duplicate = await client.post("/webhooks/gitlab", json=_pipeline(1))
    assert duplicate.status_code == 200, duplicate.text
    assert duplicate.json()["tasks_created"] == 0
    assert "deduplicated" in duplicate.json()["skipped_reason"]


@pytest.mark.anyio
async def test_unmatched_rule_preserves_merge_request_with_optional_user_shape(
    client: AsyncClient, tmp_path: Path
) -> None:
    _configure(tmp_path, cooldown=300)
    response = await client.post(
        "/webhooks/gitlab",
        json={
            "object_kind": "merge_request",
            "object_attributes": {"iid": 7, "title": "Example merge request", "action": "open"},
            "project": {"path_with_namespace": "test/widget"},
            "user": "automation",
        },
        headers={"X-Gitlab-Event": "Merge Request Hook"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["tasks_created"] == 1
    assert "skipped_reason" not in response.json()


@pytest.mark.anyio
async def test_gitlab_history_error_preserves_routing_result(
    client: AsyncClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure(tmp_path, cooldown=0)

    def unavailable(*_args, **_kwargs):
        raise OSError("history unavailable")

    monkeypatch.setattr(TriggerManager, "record_fire", unavailable)
    first = await client.post("/webhooks/gitlab", json=_pipeline(1))
    assert first.status_code == 200, first.text
    assert first.json()["tasks_created"] == 1
    duplicate = await client.post("/webhooks/gitlab", json=_pipeline(1))
    assert duplicate.status_code == 503, duplicate.text
    assert "unconfirmed" in duplicate.json()["detail"]


@pytest.mark.anyio
async def test_gitlab_cooldown_and_dedup_are_tenant_scoped(
    client: AsyncClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure(tmp_path, cooldown=300)
    monkeypatch.setattr(
        "bernstein.core.routes.webhooks.request_tenant_id", lambda request: request.headers["X-Fixture-Tenant"]
    )
    for tenant in ("first", "second"):
        response = await client.post("/webhooks/gitlab", json=_pipeline(1), headers={"X-Fixture-Tenant": tenant})
        assert response.status_code == 200, response.text
        assert response.json()["tasks_created"] == 1

    blocked = await client.post("/webhooks/gitlab", json=_pipeline(2), headers={"X-Fixture-Tenant": "first"})
    assert blocked.status_code == 200, blocked.text
    assert blocked.json()["tasks_created"] == 0
    assert "cooldown" in blocked.json()["skipped_reason"]


def test_gitlab_projection_keeps_optional_scalar_types_out_of_trigger_keys() -> None:
    from bernstein.core.routes.webhooks import _gitlab_trigger_event

    event = _gitlab_trigger_event(
        {
            "object_attributes": {"ref": [], "source_branch": "release/stable", "sha": 1},
            "project": {"path_with_namespace": [], "name": "widget"},
            "user": {"username": 1, "name": "Tester"},
        },
        "merge_request",
        "fixture",
    )
    assert (event.branch, event.sha, event.repo, event.sender) == ("release/stable", "", "widget", "Tester")


@pytest.mark.anyio
async def test_pending_gitlab_delivery_remains_unconfirmed_during_cooldown(client: AsyncClient, tmp_path: Path) -> None:
    from bernstein.core.orchestration.trigger_manager import compute_dedup_key
    from bernstein.core.routes.webhooks import _gitlab_trigger_event

    sdd_dir = _configure(tmp_path, cooldown=300)
    event = _gitlab_trigger_event(_pipeline(1), "pipeline", "default")
    key = compute_dedup_key("release-pipeline", event)
    assert TriggerManager(sdd_dir).claim_dedup(key, 300)
    confirmed = await client.post("/webhooks/gitlab", json=_pipeline(2))
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["tasks_created"] == 1

    pending = await client.post("/webhooks/gitlab", json=_pipeline(1))
    assert pending.status_code == 503, pending.text
    assert "unconfirmed" in pending.json()["detail"]
