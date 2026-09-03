"""Model-admission registry as a projection of the audit chain (issue #5038).

The registry is not a table of allowed models. It is the replay of an
append-only, HMAC-chained sequence of admit / withdraw events, so the
question "was this model permitted when that artefact was produced" has an
answer that survives every later edit -- because there are no edits.

These tests pin slices 1 and 2 of the issue: the event shapes, the
determinism of the projection, reconstruction at a past instant, expiry
without a closing event, and the fail-closed treatment of a reference the
log never admitted.
"""

from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest

from bernstein.core.lineage.entry import ModelRef
from bernstein.core.routing.model_registry import (
    ANY_TASK_CLASS,
    EVENT_MODEL_ADMITTED,
    EVENT_MODEL_WITHDRAWN,
    ModelRegistryError,
    format_timestamp,
    is_admitted,
    load_registry_events,
    model_key,
    project_registry,
    record_model_admission,
    record_model_withdrawal,
)
from bernstein.core.security.audit_chain import AuditChainStore

if TYPE_CHECKING:
    from pathlib import Path

    from bernstein.core.security.audit import AuditEvent


def _chain(tmp_path: Path, name: str = "audit") -> AuditChainStore:
    return AuditChainStore(tmp_path / name, key=b"k" * 32)


def _future(days: int) -> str:
    return format_timestamp(datetime.now(tz=UTC) + timedelta(days=days))


def _at(days: int) -> str:
    return format_timestamp(datetime.now(tz=UTC) + timedelta(days=days))


def _admit(chain: AuditChainStore, model: str, *, expires_at: str | None = None) -> AuditEvent:
    return record_model_admission(
        chain=chain,
        provider="anthropic",
        model=model,
        version=None,
        task_classes=("code", "review"),
        admitted_by="operator@example.test",
        expires_at=expires_at or _future(30),
        evidence_ref="sha256:" + "e" * 64,
    )


def _ref(model: str, *, reported: str | None = None, version: str | None = None) -> ModelRef:
    return ModelRef(
        provider="anthropic",
        model_requested=model,
        model_reported=reported,
        version=version,
    )


# 1 -------------------------------------------------------------------------


def test_admission_and_withdrawal_are_chain_events_not_mutations(tmp_path: Path) -> None:
    chain = _chain(tmp_path)
    admitted = _admit(chain, "opus")
    admitted_details = dict(admitted.details)

    withdrawn = record_model_withdrawal(
        chain=chain,
        provider="anthropic",
        model="opus",
        version=None,
        withdrawn_by="operator@example.test",
        reason="superseded",
    )

    ok, errors = chain.verify()
    assert ok, errors

    rows = load_registry_events(chain)
    assert [r.event_type for r in rows] == [EVENT_MODEL_ADMITTED, EVENT_MODEL_WITHDRAWN]
    # The withdrawal appended; it did not edit, replace or remove the
    # admission it supersedes.
    assert rows[0].details == admitted_details
    assert "prev_chain_digest" in rows[0].details
    assert "prev_chain_digest" in rows[1].details
    assert rows[1].details["reason"] == "superseded"

    # The state that held between the two events is still reconstructible
    # from the same log, which is what "not a mutation" buys.
    assert is_admitted(
        project_registry(rows, at=admitted.timestamp),
        _ref("opus"),
        task_class="code",
    )
    assert not is_admitted(
        project_registry(rows, at=withdrawn.timestamp),
        _ref("opus"),
        task_class="code",
    )


# 2 -------------------------------------------------------------------------


def test_registry_state_is_a_pure_projection_of_the_event_log(tmp_path: Path) -> None:
    chain = _chain(tmp_path)
    _admit(chain, "opus")
    _admit(chain, "haiku")
    record_model_withdrawal(
        chain=chain,
        provider="anthropic",
        model="haiku",
        version=None,
        withdrawn_by="operator@example.test",
        reason="withdrawn",
    )
    _admit(chain, "sonnet")

    rows = load_registry_events(chain)
    at = _at(0)

    baseline = project_registry(rows, at=at).canonical_bytes()

    # Same log, replayed again: byte-identical.
    assert project_registry(rows, at=at).canonical_bytes() == baseline

    # Permuted input: the projection orders the log itself, so the caller's
    # iteration order cannot change the answer.
    for seed in range(5):
        shuffled = list(rows)
        random.Random(seed).shuffle(shuffled)
        assert project_registry(shuffled, at=at).canonical_bytes() == baseline

    # Re-read from disk by a fresh reader: byte-identical.
    reread = load_registry_events(_chain(tmp_path))
    assert project_registry(reread, at=at).canonical_bytes() == baseline

    state = project_registry(rows, at=at)
    assert [a.model_key for a in state.admissions] == [
        model_key("anthropic", "opus"),
        model_key("anthropic", "sonnet"),
    ]


# 3 -------------------------------------------------------------------------


def test_registry_at_a_past_timestamp_reconstructs_the_state_that_held_then(tmp_path: Path) -> None:
    chain = _chain(tmp_path)
    before = _at(-1)
    admitted = _admit(chain, "opus")
    withdrawn = record_model_withdrawal(
        chain=chain,
        provider="anthropic",
        model="opus",
        version=None,
        withdrawn_by="operator@example.test",
        reason="withdrawn",
    )
    rows = load_registry_events(chain)

    # Before the admission the model was never permitted.
    assert project_registry(rows, at=before).admissions == ()

    # Between admission and withdrawal it was.
    held = project_registry(rows, at=admitted.timestamp)
    assert [a.model_key for a in held.admissions] == [model_key("anthropic", "opus")]
    assert held.admissions[0].admitted_by == "operator@example.test"
    assert held.admissions[0].task_classes == ("code", "review")

    # At and after the withdrawal it was not.
    assert project_registry(rows, at=withdrawn.timestamp).admissions == ()
    assert project_registry(rows, at=_at(1)).admissions == ()


# 4 -------------------------------------------------------------------------


def test_expired_admission_stops_permitting(tmp_path: Path) -> None:
    chain = _chain(tmp_path)
    admitted = _admit(chain, "opus", expires_at=_future(1))
    rows = load_registry_events(chain)

    assert is_admitted(project_registry(rows, at=admitted.timestamp), _ref("opus"), task_class="code")

    # Two days on, with no event written at expiry, the admission is gone.
    lapsed = project_registry(rows, at=_at(2))
    assert lapsed.admissions == ()
    assert not is_admitted(lapsed, _ref("opus"), task_class="code")
    # The log is unchanged: expiry is a property of the replay, not a write.
    assert len(load_registry_events(chain)) == 1


# 5 -------------------------------------------------------------------------


def test_unknown_model_ref_is_treated_as_not_admitted(tmp_path: Path) -> None:
    chain = _chain(tmp_path)
    _admit(chain, "opus")
    state = project_registry(load_registry_events(chain), at=_at(0))

    assert not is_admitted(state, _ref("some-other-model"), task_class="code")
    assert not is_admitted(state, ModelRef(provider="openai", model_requested="opus"), task_class="code")
    # An empty log admits nothing at all.
    empty = project_registry([], at=_at(0))
    assert not is_admitted(empty, _ref("opus"), task_class="code")


# 6 -------------------------------------------------------------------------


def test_reported_model_outside_the_admission_is_not_admitted(tmp_path: Path) -> None:
    chain = _chain(tmp_path)
    _admit(chain, "opus")
    state = project_registry(load_registry_events(chain), at=_at(0))

    # The provider answered with a model nobody admitted. The requested name
    # being admitted is not enough: both identities the reference presents
    # have to be admitted, or the reference is refused.
    assert is_admitted(state, _ref("opus"), task_class="code")
    assert not is_admitted(state, _ref("opus", reported="opus-preview"), task_class="code")


# 7 -------------------------------------------------------------------------


def test_task_class_outside_the_admission_is_not_admitted(tmp_path: Path) -> None:
    chain = _chain(tmp_path)
    _admit(chain, "opus")
    record_model_admission(
        chain=chain,
        provider="anthropic",
        model="haiku",
        version=None,
        task_classes=(ANY_TASK_CLASS,),
        admitted_by="operator@example.test",
        expires_at=_future(30),
    )
    state = project_registry(load_registry_events(chain), at=_at(0))

    assert is_admitted(state, _ref("opus"), task_class="review")
    assert not is_admitted(state, _ref("opus"), task_class="deploy")
    # An explicit wildcard admission covers every class.
    assert is_admitted(state, _ref("haiku"), task_class="deploy")


# 8 -------------------------------------------------------------------------


def test_projection_refuses_an_unverifiable_chain(tmp_path: Path) -> None:
    chain = _chain(tmp_path)
    _admit(chain, "opus")

    day = next(iter(sorted((tmp_path / "audit").glob("*.jsonl"))))
    tampered = day.read_text(encoding="utf-8").replace('"code"', '"deploy"')
    day.write_text(tampered, encoding="utf-8")

    with pytest.raises(ModelRegistryError):
        load_registry_events(_chain(tmp_path))


def test_pinned_admission_covers_only_its_own_snapshot(tmp_path: Path) -> None:
    chain = _chain(tmp_path)
    record_model_admission(
        chain=chain,
        provider="anthropic",
        model="opus",
        version="2026-05-01",
        task_classes=("code",),
        admitted_by="operator@example.test",
        expires_at=_future(30),
    )
    state = project_registry(load_registry_events(chain), at=_at(0))

    assert is_admitted(state, _ref("opus", version="2026-05-01"), task_class="code")
    # A pinned admission does not cover a different snapshot, nor a
    # reference that names no snapshot at all.
    assert not is_admitted(state, _ref("opus", version="2026-06-01"), task_class="code")
    assert not is_admitted(state, _ref("opus"), task_class="code")
