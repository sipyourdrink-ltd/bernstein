"""The quickstart completion poll against the real /status shape.

``bernstein quickstart`` had the same defect the demo poll did (issue
#3433, finding 3723321824): the wrapped ``tasks: {"count", "items"}``
payload was iterated raw under a broad ``suppress(Exception)``, so the
shape crash was eaten on every tick, the early exit was unreachable,
and every quickstart run burned its full timeout. These tests drive
``_poll_until_done`` through the shared unwrap and the lineage-aware
exit.
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

from bernstein.cli.commands import quickstart_cmd


def _resp(items: list[dict]) -> MagicMock:
    resp = MagicMock(status_code=200)
    resp.json.return_value = {"tasks": {"count": len(items), "items": items}}
    return resp


def test_quickstart_poll_exits_early_on_the_dict_shaped_status_payload():
    """All lineages done on the first tick must end the poll immediately -
    pre-fix the dict shape crashed row processing and the poll always ran
    to its deadline behind a blank spinner."""
    items = [
        {"id": "t1", "lineage_id": "t1", "title": "a", "role": "backend", "status": "done"},
        {"id": "t2", "lineage_id": "t2", "title": "b", "role": "qa", "status": "done"},
    ]
    with patch.object(quickstart_cmd.httpx, "get", return_value=_resp(items)):
        start = time.monotonic()
        quickstart_cmd._poll_until_done("http://127.0.0.1:1", start + 6.0, expected_total=2)
        elapsed = time.monotonic() - start
    assert elapsed < 2.0, f"poll must exit on the first tick when everything is done; took {elapsed:.1f}s"


def test_quickstart_poll_keeps_waiting_while_a_failed_lineage_may_retry():
    """A failed row is not terminal - its retry spawns later as a new row -
    so the poll must not tear the run down on done+failed."""
    items = [
        {"id": "t1", "lineage_id": "t1", "title": "a", "role": "backend", "status": "done"},
        {"id": "t2", "lineage_id": "t2", "title": "b", "role": "qa", "status": "failed"},
    ]
    with patch.object(quickstart_cmd.httpx, "get", return_value=_resp(items)):
        start = time.monotonic()
        quickstart_cmd._poll_until_done("http://127.0.0.1:1", start + 3.0, expected_total=2)
        elapsed = time.monotonic() - start
    assert elapsed >= 2.9, f"a failed lineage must keep the poll alive for its retry; exited after {elapsed:.1f}s"


def test_quickstart_poll_ignores_an_incomplete_snapshot():
    """The current snapshot's own lineage set is not a valid completion
    target: while rows are still being registered, a partial snapshot
    whose visible lineages are all done would satisfy it and tear the
    run down with work remaining (finding 3723410822). The exit must
    wait for the seeded count of distinct done lineages.
    """
    items = [
        {"id": "t1", "lineage_id": "t1", "title": "a", "role": "backend", "status": "done"},
        {"id": "t2", "lineage_id": "t2", "title": "b", "role": "qa", "status": "done"},
    ]
    with patch.object(quickstart_cmd.httpx, "get", return_value=_resp(items)):
        start = time.monotonic()
        quickstart_cmd._poll_until_done("http://127.0.0.1:1", start + 3.0, expected_total=3)
        elapsed = time.monotonic() - start
    assert elapsed >= 2.9, f"2 done of 3 seeded must keep the poll alive; exited after {elapsed:.1f}s"
