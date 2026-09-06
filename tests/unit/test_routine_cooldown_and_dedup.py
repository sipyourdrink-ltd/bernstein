"""Issue #5113: a cooldown that resets only on a non-empty result, and one action per event id.

Two defects, one file, because they are the two halves of "did this routine
actually do anything".

**The cooldown reset.** ``_last_fire_time`` returned the latest fire record of
any kind, so a routine that ran, found nothing, and recorded that fact then
suppressed itself for the whole cooldown window. An operator reading the fire
log cannot tell "nothing is happening" from "nothing is checking" -- and those
are the two states that most need telling apart.

**The dedup race.** ``_check_dedup`` read a dict loaded at construction and
``_record_dedup`` rewrote the whole file afterwards. Two ticks -- or two
processes under the schedule supervisor -- could both pass the check on the
same event id before either wrote it back, and both proceeded. That is one
action per delivery, not one per event id.
"""

from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING, Any

import pytest
import yaml
from bernstein.core.models import TriggerEvent
from bernstein.core.trigger_manager import TriggerManager, compute_dedup_key

if TYPE_CHECKING:
    from pathlib import Path

COOLDOWN_S = 3600


@pytest.fixture()
def sdd_dir(tmp_path: Path) -> Path:
    sdd = tmp_path / ".sdd"
    (sdd / "config").mkdir(parents=True)
    (sdd / "runtime" / "triggers").mkdir(parents=True)
    config: dict[str, Any] = {
        "version": 1,
        "triggers": [
            {
                "name": "nightly-sweep",
                "source": "cron",
                "enabled": True,
                "conditions": {"cooldown_s": COOLDOWN_S},
                "task": {"title": "sweep", "description": "sweep the repo"},
            }
        ],
    }
    with (sdd / "config" / "triggers.yaml").open("w") as handle:
        yaml.dump(config, handle)
    return sdd


def _event(marker: str, *, at: float | None = None) -> TriggerEvent:
    """A cron event. `compute_dedup_key` buckets cron by the minute, so two
    events meant to be distinct have to sit in different 60s buckets."""
    return TriggerEvent(
        source="cron",
        timestamp=time.time() if at is None else at,
        raw_payload={"trigger_name": "nightly-sweep", "marker": marker},
        message=f"Cron trigger: {marker}",
    )


def _fire_log(sdd_dir: Path) -> list[dict[str, Any]]:
    path = sdd_dir / "runtime" / "triggers" / "fire_log.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line]


# ---------------------------------------------------------------------------
# The cooldown resets only on a productive run
# ---------------------------------------------------------------------------


def test_an_empty_run_does_not_start_the_cooldown(sdd_dir: Path) -> None:
    """Three consecutive empty runs are three runs, not one run and two skips."""
    mgr = TriggerManager(sdd_dir)
    base = time.time()
    for i in range(3):
        payloads, suppressed = mgr.evaluate(_event(f"empty-{i}", at=base + i * 120))
        assert len(payloads) == 1, f"run {i} was suppressed: {suppressed}"
        mgr.record_fire("nightly-sweep", "cron", f"task-{i}", f"dedup-{i}", "nothing found", produced=False)

    # All three are visible to an operator...
    assert len(_fire_log(sdd_dir)) == 3
    assert [entry["produced"] for entry in _fire_log(sdd_dir)] == [False, False, False]
    # ...and none of them moved the clock.
    assert mgr._last_fire_time("nightly-sweep") is None


def test_a_productive_run_starts_the_cooldown(sdd_dir: Path) -> None:
    """The behaviour that must not change: a fire that found work suppresses the next."""
    mgr = TriggerManager(sdd_dir)
    base = time.time()
    payloads, _ = mgr.evaluate(_event("first", at=base))
    assert len(payloads) == 1
    mgr.record_fire("nightly-sweep", "cron", "task-1", "dedup-1", "two tasks created")

    payloads, suppressed = mgr.evaluate(_event("second", at=base + 120))
    assert payloads == []
    assert "cooldown" in suppressed.get("nightly-sweep", "")


def test_an_empty_run_after_a_productive_one_leaves_the_clock_alone(sdd_dir: Path) -> None:
    """An empty run must not EXTEND a cooldown either -- it is not a fire at all."""
    mgr = TriggerManager(sdd_dir)
    mgr.record_fire("nightly-sweep", "cron", "task-1", "dedup-1", "found work")
    productive_at = mgr._last_fire_time("nightly-sweep")
    assert productive_at is not None

    time.sleep(0.01)
    mgr.record_fire("nightly-sweep", "cron", "task-2", "dedup-2", "nothing found", produced=False)
    assert mgr._last_fire_time("nightly-sweep") == productive_at


def test_a_record_written_before_the_field_existed_still_counts(sdd_dir: Path) -> None:
    """An old fire_log line has no `produced` key, and was a real fire."""
    path = sdd_dir / "runtime" / "triggers" / "fire_log.jsonl"
    legacy = {
        "trigger_name": "nightly-sweep",
        "source": "cron",
        "fired_at": 1_700_000_000.0,
        "task_id": "task-old",
        "dedup_key": "dedup-old",
        "event_summary": "from before #5113",
    }
    path.write_text(json.dumps(legacy) + "\n")
    assert TriggerManager(sdd_dir)._last_fire_time("nightly-sweep") == 1_700_000_000.0


# ---------------------------------------------------------------------------
# One action per event id, decided atomically
# ---------------------------------------------------------------------------


def test_the_same_event_id_twice_produces_one_action(sdd_dir: Path) -> None:
    mgr = TriggerManager(sdd_dir)
    event = _event("same-id")

    payloads, _ = mgr.evaluate(event)
    assert len(payloads) == 1

    payloads, suppressed = mgr.evaluate(event)
    assert payloads == []
    assert suppressed.get("nightly-sweep") == "deduplicated"


def test_a_second_process_cannot_claim_a_key_the_first_holds(sdd_dir: Path) -> None:
    """The race the split check-then-write allowed, as two managers on one runtime dir.

    A second ``TriggerManager`` loads its own cache at construction, exactly as
    a second process under the schedule supervisor does. Before the claim was
    atomic, both saw the key absent and both proceeded.
    """
    first = TriggerManager(sdd_dir)
    key = compute_dedup_key("nightly-sweep", _event("shared"))
    assert first.claim_dedup(key, COOLDOWN_S) is True

    second = TriggerManager(sdd_dir)
    assert second.claim_dedup(key, COOLDOWN_S) is False


def test_only_one_of_many_concurrent_claims_wins(sdd_dir: Path) -> None:
    """Eight threads, one key, one winner -- the check and the write are one section."""
    managers = [TriggerManager(sdd_dir) for _ in range(8)]
    key = compute_dedup_key("nightly-sweep", _event("contended"))

    with ThreadPoolExecutor(max_workers=len(managers)) as pool:
        results = list(pool.map(lambda mgr: mgr.claim_dedup(key, COOLDOWN_S), managers))

    assert sum(results) == 1, f"{sum(results)} callers claimed the same event id"


def test_an_expired_claim_can_be_taken_again(sdd_dir: Path) -> None:
    """A claim is a window, not a tombstone."""
    mgr = TriggerManager(sdd_dir)
    key = compute_dedup_key("nightly-sweep", _event("short-ttl"))
    assert mgr.claim_dedup(key, 0) is True
    assert mgr.claim_dedup(key, COOLDOWN_S) is True


def test_a_failed_render_does_not_burn_the_reservation(sdd_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The claim is taken AFTER the payload renders, not before.

    Claiming on the early advisory check would mean a template error silently
    swallowed the event for the whole cooldown window, with nothing created and
    no way to retry it -- the reservation outliving the thing it reserved.
    """
    from bernstein.core.orchestration import trigger_manager as module

    def _explode(*_args: object, **_kwargs: object) -> dict[str, Any]:
        raise ValueError("no such field")

    monkeypatch.setattr(module, "render_task_payload", _explode)

    mgr = TriggerManager(sdd_dir)
    event = _event("render-fails")
    payloads, suppressed = mgr.evaluate(event)
    assert payloads == []
    assert "template_error" in suppressed.get("nightly-sweep", "")

    # The key is still free, so a fixed template on the next tick can use it.
    assert mgr.claim_dedup(compute_dedup_key("nightly-sweep", event), COOLDOWN_S) is True


def test_the_dedup_cache_survives_an_interrupted_write(sdd_dir: Path) -> None:
    """Written through a scratch sibling, so a torn file cannot release every claim."""
    mgr = TriggerManager(sdd_dir)
    key = compute_dedup_key("nightly-sweep", _event("durable"))
    assert mgr.claim_dedup(key, COOLDOWN_S) is True

    cache_path = sdd_dir / "runtime" / "triggers" / "dedup_cache.json"
    assert json.loads(cache_path.read_text())[key] > time.time()
    assert not list(cache_path.parent.glob("dedup_cache.*.tmp")), "scratch file left behind"
