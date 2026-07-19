"""The ``recipe.fire`` receipt attests work that was actually accepted (#2654).

A fire receipt is an HMAC-chained claim that work happened. These tests pin
the properties that make the claim true rather than merely present:

- the registry never fabricates a dispatcher, so a fire cannot be counted as
  dispatched by a component that only *renders* candidate work;
- the receipt records the identifiers the dispatcher returned, so an auditor
  can check the work exists rather than trusting a bare count;
- the verdict is a function of the fire's inputs, not of a mutable
  time-windowed cache;
- "paused" is structured state, not a substring of a prose reason.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from bernstein.core.security.audit_chain import EVENT_RECIPE_FIRE, AuditChainStore
from bernstein.core.workflows.recipe_registry import (
    RecipePins,
    RecipeRegistry,
)
from bernstein.core.workflows.recipe_spec import load_recipe_spec_from_text

if TYPE_CHECKING:
    from pathlib import Path

_KEY = b"0" * 32

_MANIFEST = """
name: nightly-triage
description: Nightly triage recipe
version: "1.0.0"
nodes:
  - id: triage
    command: "echo triage"
"""

#: A trigger rule that matches a schedule fire. Its presence must not, on its
#: own, make a fire count as dispatched: matching a rule renders a candidate
#: payload, it does not submit work.
_TRIGGERS_YAML = (
    "triggers:\n"
    "  - name: recipe-fire\n"
    "    source: schedule\n"
    "    enabled: true\n"
    "    task:\n"
    '      title: "Recipe fire"\n'
    "      role: backend\n"
)


def _registry(sdd: Path, *, dispatch: Any = None) -> RecipeRegistry:
    sdd.mkdir(parents=True, exist_ok=True)
    return RecipeRegistry(
        sdd,
        chain=AuditChainStore(sdd / "audit", key=_KEY),
        hmac_key=_KEY,
        lineage_key=_KEY,
        dispatch=dispatch,
    )


def _fire_receipts(reg: RecipeRegistry) -> list[Any]:
    return list(reg._get_chain().query(event_type=EVENT_RECIPE_FIRE))


def _register(reg: RecipeRegistry) -> Any:
    return reg.register(spec=load_recipe_spec_from_text(_MANIFEST), pins=RecipePins(git_commit="c1"))


# ---------------------------------------------------------------------------
# the registry never fabricates a dispatcher
# ---------------------------------------------------------------------------


class TestNoImplicitDispatcher:
    def test_matching_trigger_rules_alone_do_not_count_as_submitted_work(self, tmp_path: Path) -> None:
        """A rendered trigger payload is a candidate, not submitted work.

        Regression for the receipt being a false attestation: the registry
        used to resolve a default dispatcher over ``TriggerManager.evaluate``,
        which only renders payloads, and counted them as submissions.
        """
        sdd = tmp_path / ".sdd"
        (sdd / "config").mkdir(parents=True)
        (sdd / "config" / "triggers.yaml").write_text(_TRIGGERS_YAML, encoding="utf-8")

        reg = _registry(sdd)
        _register(reg)
        result = reg.fire("nightly-triage", fire_time=1_800_000_000)

        assert result.dispatched is False, "a matched trigger rule is not submitted work"
        assert result.submitted == 0
        assert result.reason
        assert _fire_receipts(reg) == [], "no receipt may attest work that was never submitted"

    def test_registry_without_a_dispatcher_fails_closed(self, tmp_path: Path) -> None:
        reg = _registry(tmp_path / ".sdd")
        _register(reg)
        result = reg.fire("nightly-triage", fire_time=1_800_000_000)

        assert result.dispatched is False
        assert "dispatcher" in result.reason
        assert _fire_receipts(reg) == []


# ---------------------------------------------------------------------------
# the receipt records what was accepted
# ---------------------------------------------------------------------------


class TestReceiptRecordsAcceptedWork:
    def test_receipt_names_the_submitted_work_items(self, tmp_path: Path) -> None:
        reg = _registry(tmp_path / ".sdd", dispatch=lambda _e: ["T-101", "T-102"])
        registered = _register(reg)
        result = reg.fire("nightly-triage", fire_time=1_800_000_000)

        assert result.dispatched is True
        assert result.submitted == 2
        assert result.submitted_ids == ("T-101", "T-102")

        receipts = _fire_receipts(reg)
        assert len(receipts) == 1
        details = dict(receipts[0].details)
        assert details["submitted"] == 2
        # The identifiers are the evidence: an auditor can go look for them.
        assert details["submitted_ids"] == ["T-101", "T-102"]
        assert details["recipe_hash"] == registered.recipe_hash

    def test_dispatcher_returning_nothing_writes_no_receipt(self, tmp_path: Path) -> None:
        reg = _registry(tmp_path / ".sdd", dispatch=lambda _e: [])
        _register(reg)
        result = reg.fire("nightly-triage", fire_time=1_800_000_000)

        assert result.dispatched is False
        assert result.submitted == 0
        assert _fire_receipts(reg) == []

    @pytest.mark.parametrize("bogus", [1, True, "T-1", None, object()])
    def test_non_identifier_returns_are_not_evidence(self, tmp_path: Path, bogus: Any) -> None:
        """Only a sequence of identifiers counts.

        A bare int (or ``True``, or a bare string that would otherwise iterate
        into characters) is not proof that anything was accepted.
        """
        reg = _registry(tmp_path / ".sdd", dispatch=lambda _e: bogus)
        _register(reg)
        result = reg.fire("nightly-triage", fire_time=1_800_000_000)

        assert result.dispatched is False, f"{bogus!r} must not be read as submitted work"
        assert _fire_receipts(reg) == []

    def test_dispatcher_failure_writes_no_receipt(self, tmp_path: Path) -> None:
        def _boom(_event: Any) -> list[str]:
            raise RuntimeError("pipeline down")

        reg = _registry(tmp_path / ".sdd", dispatch=_boom)
        _register(reg)
        result = reg.fire("nightly-triage", fire_time=1_800_000_000)

        assert result.dispatched is False
        assert "pipeline down" in result.reason
        assert _fire_receipts(reg) == []


# ---------------------------------------------------------------------------
# the verdict is a function of the fire's inputs
# ---------------------------------------------------------------------------


class TestFireVerdictIsDeterministic:
    def test_identical_fires_produce_identical_verdicts(self, tmp_path: Path) -> None:
        """Two identical fires agree.

        Regression for the verdict depending on the trigger dedup cache: the
        same fire rendered one payload on the first call and zero within the
        300s cooldown, so the chain contents depended on wall-clock time.
        """
        sdd = tmp_path / ".sdd"
        (sdd / "config").mkdir(parents=True)
        (sdd / "config" / "triggers.yaml").write_text(_TRIGGERS_YAML, encoding="utf-8")

        calls: list[Any] = []

        def _dispatch(event: Any) -> list[str]:
            calls.append(event)
            return [f"T-{len(calls)}"]

        reg = _registry(sdd, dispatch=_dispatch)
        _register(reg)

        first = reg.fire("nightly-triage", fire_time=1_800_000_000)
        second = reg.fire("nightly-triage", fire_time=1_800_000_000)

        assert first.dispatched is True
        assert second.dispatched is True, "an identical fire must not be suppressed by a time-windowed cache"
        assert first.projection_hash == second.projection_hash
        assert len(_fire_receipts(reg)) == 2


# ---------------------------------------------------------------------------
# paused is structured state
# ---------------------------------------------------------------------------


class TestPausedIsStructured:
    def test_paused_fire_sets_the_paused_flag(self, tmp_path: Path) -> None:
        reg = _registry(tmp_path / ".sdd", dispatch=lambda _e: ["T-1"])
        _register(reg)
        reg.pause("nightly-triage")
        result = reg.fire("nightly-triage", fire_time=1_800_000_000)

        assert result.dispatched is False
        assert result.paused is True

    def test_dispatch_failure_mentioning_paused_is_not_paused(self, tmp_path: Path) -> None:
        """A failure reason is prose; it must not be parsed for control flow.

        Regression for the CLI deciding its exit code with ``"paused" not in
        reason``, which let any dispatcher error whose text happened to
        contain the word exit 0.
        """

        def _boom(_event: Any) -> list[str]:
            raise RuntimeError("worker queue paused for maintenance")

        reg = _registry(tmp_path / ".sdd", dispatch=_boom)
        _register(reg)
        result = reg.fire("nightly-triage", fire_time=1_800_000_000)

        assert result.dispatched is False
        assert "paused" in result.reason
        assert result.paused is False, "a failed submission is not a paused recipe"
