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

from bernstein.core.tasks.models import CompletionSignal, Complexity, Scope, Task, TaskStatus, TaskType

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
        # /tasks/{id}/fail and similar - return a benign 200.
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
    # The exhaustion path asks the store before recording, so a bare Mock
    # would answer with a truthy Mock and suppress the very call this
    # test asserts on (#3628).
    quarantine.is_quarantined.return_value = False

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
    # Title is not mutated - no stripping of a legacy prefix needed.
    assert recorded_title == task.title


def test_maybe_retry_enforces_hard_ceiling_matching_retry_or_fail():
    """`maybe_retry_task` must apply the same `_MAX_REGULAR_TASK_RETRIES=2`
    hard ceiling that `retry_or_fail_task` already enforces.

    Regression for #2806: the tick-loop retry path (`maybe_retry_task`) and
    the reap path (`retry_or_fail_task`) disagreed on the cap. A task at
    retry_count=2 with a higher `max_retries`/`max_task_retries` was refused
    by the reap path but still retried by the tick path, so a
    structurally-dead lineage could exceed the intended ceiling.
    """
    from bernstein.core.task_lifecycle import _MAX_REGULAR_TASK_RETRIES

    assert _MAX_REGULAR_TASK_RETRIES == 2
    # retry_count already at the hard ceiling, but task.max_retries (3) and
    # max_task_retries (3) would both otherwise permit another attempt.
    task = _build_task(retry_count=2, max_retries=3)
    client, posted = _capture_client()
    quarantine = MagicMock()
    # The exhaustion path asks the store before recording, so a bare Mock
    # would answer with a truthy Mock and suppress the very call this
    # test asserts on (#3628).
    quarantine.is_quarantined.return_value = False

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


def test_maybe_retry_preserves_completion_signals_owned_files_and_depends_on(tmp_path):
    """#6145: the tick-loop retry path must carry the same three fields
    forward as retry_or_fail_task's reap path does, so a task's verification
    signal and file/dependency lineage aren't dropped by whichever retry
    path happens to fire.
    """
    task = _build_task(retry_count=0)
    task.completion_signals = [CompletionSignal(type="test_passes", value="pytest tests/test_widget.py")]
    task.owned_files = ["src/widget.py"]
    task.depends_on = ["T-PARENT"]
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
    body = posted[0]
    assert body["completion_signals"] == [{"type": "test_passes", "value": "pytest tests/test_widget.py"}]
    assert body["owned_files"] == ["src/widget.py"]
    assert body["depends_on"] == ["T-PARENT"]


def test_maybe_retry_omits_completion_signals_when_task_has_none(tmp_path):
    """Mirrors retry_or_fail_task: an empty list is dropped, not posted."""
    task = _build_task(retry_count=0)
    assert task.completion_signals == []
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

    assert "completion_signals" not in posted[0]
    assert posted[0]["owned_files"] == []
    assert posted[0]["depends_on"] == []


# The fields both retry bodies must carry forward VERBATIM from the source
# task, regardless of whatever else legitimately differs between the two
# paths (effort/model escalation tiers, backoff timing, task_type: those
# are retry-policy decisions each path makes independently and on purpose,
# not lineage). #6145 was three of these five silently missing from
# maybe_retry_task's body; the other two (role, priority) already agreed
# and are pinned here so a future edit can't quietly drop them too.
_RETRY_BODY_LINEAGE_FIELDS = ("role", "priority", "owned_files", "depends_on")


def test_retry_bodies_agree_on_lineage_fields(tmp_path):
    """Both retry-body constructions must keep carrying the same task
    lineage forward, field for field.

    #6145 was exactly this: completion_signals/owned_files/depends_on were
    present in retry_or_fail_task's body and silently absent from
    maybe_retry_task's, with nothing catching the drift until someone
    noticed the missing lineage in production. This pins "these two bodies
    carry the same lineage" as a property CI holds rather than a
    coincidence a reviewer happens to catch.
    """
    task = _build_task(retry_count=0)
    task.completion_signals = [CompletionSignal(type="test_passes", value="pytest tests/test_widget.py")]
    task.owned_files = ["src/widget.py"]
    task.depends_on = ["T-PARENT"]

    tick_client, tick_posted = _capture_client()
    maybe_retry_task(
        task,
        retried_task_ids=set(),
        max_task_retries=3,
        client=tick_client,
        server_url="http://server",
        quarantine=MagicMock(),
        workdir=tmp_path,
        session_id=None,
    )
    tick_body = tick_posted[0]

    reap_client, reap_posted = _capture_client()
    retry_or_fail_task(
        task.id,
        "agent died",
        client=reap_client,
        server_url="http://server",
        max_task_retries=3,
        retried_task_ids=set(),
        tasks_snapshot={"failed": [task]},
        workdir=tmp_path,
    )
    reap_body = reap_posted[0]

    for field in _RETRY_BODY_LINEAGE_FIELDS:
        assert field in tick_body, f"maybe_retry_task dropped {field!r}"
        assert field in reap_body, f"retry_or_fail_task dropped {field!r}"
        assert tick_body[field] == reap_body[field] == getattr(task, field), (
            f"{field!r} diverges: tick={tick_body[field]!r} reap={reap_body[field]!r} task={getattr(task, field)!r}"
        )

    # completion_signals is the odd one out: both paths omit it entirely
    # (rather than posting an empty list) when the task has none, so "in
    # the body" is itself the carried-forward signal, checked and pinned by
    # test_maybe_retry_preserves_completion_signals_owned_files_and_depends_on.
    # Here both are non-empty.
    expected_signals = [{"type": "test_passes", "value": "pytest tests/test_widget.py"}]
    assert tick_body["completion_signals"] == reap_body["completion_signals"] == expected_signals

    assert tick_body["metadata"]["retry_of"] == reap_body["metadata"]["retry_of"] == task.id


# ---------------------------------------------------------------------------
# Planning-retry race guard (#4309), tick-loop path.
#
# maybe_retry_task and retry_or_fail_task are independent retry-creation
# call sites for the same failed task (see the hard-ceiling comment above
# and retry_or_fail_task's own docstring) -- both must refuse to leave a
# planning-role retry claimable once a sibling planning task sharing its
# decomposition lineage has already finished. This function has no
# tasks_snapshot to consult, so the guard falls back to a live GET.
# ---------------------------------------------------------------------------


def _capture_client_with_done_sibling(sibling: Task) -> tuple[MagicMock, list[dict]]:
    """Like _capture_client, but GET /tasks?status=done returns *sibling*."""
    client, posted = _capture_client()

    def get_side_effect(url: str, params: dict | None = None, **_: object) -> MagicMock:
        resp = MagicMock()
        resp.raise_for_status.return_value = None
        status = (params or {}).get("status")
        resp.json.return_value = [sibling.to_dict()] if status == "done" else []
        return resp

    client.get.side_effect = get_side_effect
    return client, posted


def test_maybe_retry_planning_task_cancelled_when_sibling_done(tmp_path):
    """A planning ('manager') task's retry is created and then immediately
    cancelled once a sibling sharing its decomposition lineage is DONE."""
    task = _build_task(task_id="planning-A", retry_count=0)
    task.role = "manager"

    sibling = _build_task(task_id="planning-B", retry_count=0)
    sibling.role = "manager"
    sibling.status = TaskStatus.DONE
    sibling.metadata = {"original_task_id": "planning-A"}

    client, posted = _capture_client_with_done_sibling(sibling)

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
    assert len(posted) == 1  # the retry row was still created -- visible on the board

    cancel_calls = [call for call in client.post.call_args_list if call[0][0].endswith("/NEW-1/cancel")]
    assert cancel_calls, f"expected the duplicate retry to be cancelled, got: {client.post.call_args_list}"
    assert "planning-B" in cancel_calls[0].kwargs["json"]["reason"]


def test_maybe_retry_planning_task_unaffected_when_no_sibling_done(tmp_path):
    """Control: no completed sibling -> the planning retry stays open."""
    task = _build_task(task_id="planning-C", retry_count=0)
    task.role = "manager"
    client, posted = _capture_client()  # GET is unmocked; MagicMock default -> no siblings found

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
    assert not any(call[0][0].endswith("/cancel") for call in client.post.call_args_list)


# ---------------------------------------------------------------------------
# retry_or_fail_task
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("retry_count", "max_retries", "expected_posts", "expected_retry_count"),
    [
        (0, 3, 1, 1),
        (1, 3, 1, 2),
        # _MAX_REGULAR_TASK_RETRIES=2 hard ceiling: after 2 retries a task
        # is dead-lettered regardless of task.max_retries or dynamic_limit.
        (2, 3, 0, None),
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
    """``[retry:N]`` description marker is ignored - the typed field wins."""
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
    # The description is passed through verbatim - no new marker is added,
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
