"""Regression tests for audit-017: retry counter consolidation.

The retry counter used to live in three independent stores (title prefix
``[RETRY N]``, description marker ``[retry:N]`` and the typed
``Task.retry_count`` field).  Drift between them caused tasks to recycle
without ever hitting the DLQ.  These tests lock in that:

* Every retry path writes the typed ``retry_count`` field and leaves the
  title / description untouched (no new prefixes).
* The typed field is the single source of truth; readers fall back to
  legacy regex only when the field is zero.
* The DLQ threshold (``fail_task`` with "Max retries exceeded") fires from
  the typed field alone.
"""

from __future__ import annotations

import re
from unittest.mock import MagicMock

import httpx
import pytest
from bernstein.core.task_lifecycle import maybe_retry_task, retry_or_fail_task

from bernstein.core.tasks.models import Complexity, Scope, Task, TaskStatus, TaskType

_RETRY_PREFIX_RE = re.compile(r"\[RETRY\s+\d+\]|\[retry:\d+\]")


def _build_task(
    *,
    task_id: str = "T-1",
    title: str = "Implement widget",
    description: str = "Write the widget code.",
    retry_count: int = 0,
    max_retries: int = 3,
) -> Task:
    return Task(
        id=task_id,
        title=title,
        description=description,
        role="backend",
        status=TaskStatus.FAILED,
        scope=Scope.MEDIUM,
        complexity=Complexity.MEDIUM,
        task_type=TaskType.STANDARD,
        retry_count=retry_count,
        max_retries=max_retries,
        estimated_minutes=10,
        model="sonnet",
        effort="high",
    )


def _capture_client() -> tuple[MagicMock, list[dict]]:
    """Return a mock httpx client and a list that collects POST bodies."""
    posted: list[dict] = []

    def post_side_effect(url: str, json: dict | None = None, **_: object) -> MagicMock:
        if url.endswith("/tasks"):
            assert json is not None
            posted.append(json)
            resp = MagicMock()
            resp.raise_for_status.return_value = None
            resp.json.return_value = {"id": f"NEW-{len(posted)}"}
            resp.status_code = 201
            return resp
        # /tasks/{id}/fail and similar — return a benign 200.
        resp = MagicMock()
        resp.raise_for_status.return_value = None
        resp.status_code = 200
        return resp

    client = MagicMock(spec=httpx.Client)
    client.post.side_effect = post_side_effect
    return client, posted


# ---------------------------------------------------------------------------
# maybe_retry_task
# ---------------------------------------------------------------------------


def test_maybe_retry_increments_typed_field_only(tmp_path):
    task = _build_task(retry_count=0)
    client, posted = _capture_client()

    created = maybe_retry_task(
        task,
        retried_task_ids=set(),
        max_task_retries=3,
        client=client,
        server_url="http://server",
        quarantine=MagicMock(),
        workdir=tmp_path,
        session_id=None,
    )

    assert created is True
    assert len(posted) == 1
    body = posted[0]
    assert body["retry_count"] == 1
    assert body["title"] == task.title
    assert body["description"] == task.description
    assert not _RETRY_PREFIX_RE.search(body["title"])
    assert not _RETRY_PREFIX_RE.search(body["description"])


def test_maybe_retry_bumps_from_typed_field_across_attempts(tmp_path):
    task = _build_task(retry_count=1)
    client, posted = _capture_client()

    maybe_retry_task(
        task,
        retried_task_ids=set(),
        max_task_retries=3,
        client=client,
        server_url="http://server",
        quarantine=MagicMock(),
        workdir=tmp_path,
        session_id=None,
    )

    assert posted[0]["retry_count"] == 2
    # Title / description untouched across multiple retries.
    assert posted[0]["title"] == task.title
    assert posted[0]["description"] == task.description


def test_maybe_retry_dlq_fires_from_typed_field():
    task = _build_task(retry_count=3, max_retries=3)
    client, posted = _capture_client()
    quarantine = MagicMock()

    created = maybe_retry_task(
        task,
        retried_task_ids=set(),
        max_task_retries=3,
        client=client,
        server_url="http://server",
        quarantine=quarantine,
        workdir=None,
        session_id=None,
    )

    assert created is False
    assert posted == []
    quarantine.record_failure.assert_called_once()
    (recorded_title, _reason) = quarantine.record_failure.call_args.args
    # Title is not mutated — no stripping of a legacy prefix needed.
    assert recorded_title == task.title


def test_maybe_retry_ignores_legacy_title_prefix_when_typed_field_disagrees():
    """Legacy ``[RETRY N]`` prefix must not raise the counter."""
    task = _build_task(
        retry_count=0,
        title="[RETRY 2] Stale prefix from pre-audit-017 data",
    )
    client, posted = _capture_client()

    created = maybe_retry_task(
        task,
        retried_task_ids=set(),
        max_task_retries=3,
        client=client,
        server_url="http://server",
        quarantine=MagicMock(),
        workdir=None,
        session_id=None,
    )

    assert created is True
    # retry_count derived from the typed field (0) -> next attempt = 1,
    # NOT 3 as the legacy prefix would suggest.
    assert posted[0]["retry_count"] == 1


# ---------------------------------------------------------------------------
# retry_or_fail_task
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("retry_count", "max_retries", "expected_posts", "expected_retry_count"),
    [
        (0, 3, 1, 1),
        (1, 3, 1, 2),
        (2, 3, 1, 3),
        (3, 3, 0, None),  # DLQ: at limit => fail with "Max retries exceeded".
    ],
)
def test_retry_or_fail_task_uses_typed_field_and_triggers_dlq(
    retry_count: int,
    max_retries: int,
    expected_posts: int,
    expected_retry_count: int | None,
):
    task = _build_task(retry_count=retry_count, max_retries=max_retries)
    tasks_snapshot = {"failed": [task]}
    client, posted = _capture_client()

    retry_or_fail_task(
        task.id,
        "agent died",
        client=client,
        server_url="http://server",
        max_task_retries=max_retries,
        retried_task_ids=set(),
        tasks_snapshot=tasks_snapshot,
    )

    assert len(posted) == expected_posts
    if expected_posts:
        body = posted[0]
        assert body["retry_count"] == expected_retry_count
        # audit-017: title and description must not carry a counter prefix.
        assert body["title"] == task.title
        assert body["description"] == task.description
        assert not _RETRY_PREFIX_RE.search(body["title"])
        assert not _RETRY_PREFIX_RE.search(body["description"])
    else:
        # DLQ path: the task is failed with a "Max retries exceeded" reason
        # rather than being recreated.  The fail endpoint is hit via POST.
        fail_calls = [call for call in client.post.call_args_list if call.args and "/fail" in call.args[0]]
        assert fail_calls, "DLQ threshold did not hit the fail endpoint"
        reasons = [call.kwargs.get("json", {}).get("reason", "") for call in fail_calls]
        assert any("Max retries exceeded" in reason for reason in reasons), (
            f"DLQ threshold did not fire with 'Max retries exceeded' (got: {reasons!r})"
        )


def test_retry_or_fail_task_does_not_consult_description_marker():
    """``[retry:N]`` description marker is ignored — the typed field wins."""
    task = _build_task(
        retry_count=0,
        description="[retry:7] Stale marker from pre-audit-017 data.",
    )
    tasks_snapshot = {"failed": [task]}
    client, posted = _capture_client()

    retry_or_fail_task(
        task.id,
        "agent died",
        client=client,
        server_url="http://server",
        max_task_retries=3,
        retried_task_ids=set(),
        tasks_snapshot=tasks_snapshot,
    )

    assert len(posted) == 1
    # Counter came from the typed field (0 -> 1), not the marker (7).
    assert posted[0]["retry_count"] == 1
    # The description is passed through verbatim — no new marker is added,
    # and the stale one is not stripped (migration-safe).
    assert posted[0]["description"] == task.description


def test_retry_or_fail_task_preserves_lineage_in_metadata():
    task = _build_task(retry_count=0)
    tasks_snapshot = {"failed": [task]}
    client, posted = _capture_client()

    retry_or_fail_task(
        task.id,
        "agent died",
        client=client,
        server_url="http://server",
        max_task_retries=3,
        retried_task_ids=set(),
        tasks_snapshot=tasks_snapshot,
    )

    meta = posted[0]["metadata"]
    # Lineage is tracked in metadata so downstream consumers (e.g. the
    # compaction patcher) can find the retry task without string matching.
    assert meta.get("original_task_id") == task.id
