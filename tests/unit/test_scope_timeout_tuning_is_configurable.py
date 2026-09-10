"""`tuning.task.scope_timeout_s` must reach the value the watchdog is armed with.

`_batch_timeout_seconds` resolves the wall-clock bucket a spawn is given, and
`adapter.spawn(timeout_seconds=...)` arms a one-shot timer from it, so this
function decides when an agent is killed.

It read the `TASK` singleton through a name bound with `from ... import` at
module import. `bernstein.yaml`'s `tuning:` block is applied much later by
`config.seed_parser._parse_tuning` -> `defaults.override`, which REBINDS the
module attribute rather than mutating the frozen instance (`defaults.override`
says so in its own docstring). A name captured at import is therefore a
permanent snapshot of the shipped defaults, and a retuned bucket reached the
config snapshot but never the kill path.

These drive `defaults.override` - the real production path - rather than
monkeypatching the module attribute, which is what let the defect through:
patching `task_lifecycle.TASK` passes against the broken build.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from bernstein.core import defaults
from bernstein.core.tasks.models import Task
from bernstein.core.tasks.task_lifecycle import _batch_timeout_seconds


@pytest.fixture(autouse=True)
def _restore_defaults() -> Iterator[None]:
    """Tuning singletons are process-global; put them back after each test."""
    yield
    defaults.reset()


def _task(scope: str = "medium", role: str = "resolver") -> Task:
    return Task.from_dict(
        {"id": "t1", "title": "t", "description": "d", "role": role, "status": "open", "scope": scope}
    )


class TestDefaultIsUnchanged:
    """Left at the shipped defaults the buckets must resolve exactly as before."""

    @pytest.mark.parametrize(("scope", "expected"), [("small", 900), ("medium", 1800), ("large", 3600)])
    def test_scope_buckets_are_untouched(self, scope: str, expected: int) -> None:
        assert _batch_timeout_seconds([_task(scope)]) == expected


class TestTuningReachesTheBuckets:
    def test_a_retuned_scope_bucket_is_honoured(self) -> None:
        """Fails on a build that captured TASK at import: still returns 1800."""
        defaults.override("task", {"scope_timeout_s": {"medium": 2700.0}})
        assert _batch_timeout_seconds([_task("medium")]) == 2700

    def test_an_untouched_scope_keeps_its_shipped_bucket(self) -> None:
        """`override` merges mapping fields, so retuning one scope leaves the rest."""
        defaults.override("task", {"scope_timeout_s": {"medium": 2700.0}})
        assert _batch_timeout_seconds([_task("small")]) == 900

    def test_a_retuned_xl_timeout_is_honoured(self) -> None:
        """The xl branch read the same stale singleton."""
        defaults.override("task", {"xl_timeout_s": 9000.0})
        assert _batch_timeout_seconds([_task("large", role="architect")]) == 9000
