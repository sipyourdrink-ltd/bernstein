"""Regression: tasks without a CachePolicy behave exactly as today (AC7).

A task that declares no ``cache_policy`` must parse to ``None`` and be
indistinguishable from a pre-feature task on every other field, so the spawn
path is byte-identical to current behaviour.
"""

from __future__ import annotations

import dataclasses

from bernstein.core.persistence.cache_policy import CachePolicy, refresh_requested
from bernstein.core.tasks.models import Task

# Pin created_at so two from_dict calls do not diverge on the time.time default.
_BASE = {
    "id": "t-1",
    "title": "Add login",
    "description": "Do the thing",
    "role": "backend",
    "created_at": 1_700_000_000.0,
}


def test_task_without_policy_parses_to_none() -> None:
    task = Task.from_dict(dict(_BASE))
    assert task.cache_policy is None
    assert CachePolicy.from_task(task) is None


def test_task_without_policy_matches_all_other_fields() -> None:
    # Building the same task via from_dict with and without the (absent) key
    # yields identical dataclasses - the field is purely additive.
    a = Task.from_dict(dict(_BASE))
    b = Task.from_dict({**_BASE, "cache_policy": None})
    assert dataclasses.asdict(a) == dataclasses.asdict(b)


def test_task_with_policy_parses_and_resolves() -> None:
    task = Task.from_dict(
        {
            **_BASE,
            "cache_policy": {
                "ingredients": ["task_inputs"],
                "expiry_mode": "drift",
                "drift_window": 3,
                "verified_only": True,
            },
        }
    )
    policy = CachePolicy.from_task(task)
    assert policy is not None
    assert policy.verified_only is True
    assert policy.drift_window == 3
    assert policy.ingredients == ("task_inputs",)


def test_refresh_requested_reads_env() -> None:
    assert refresh_requested({"BERNSTEIN_REFRESH_CACHE": "1"}) is True
    assert refresh_requested({"BERNSTEIN_REFRESH_CACHE": "true"}) is True
    assert refresh_requested({"BERNSTEIN_REFRESH_CACHE": "0"}) is False
    assert refresh_requested({}) is False
