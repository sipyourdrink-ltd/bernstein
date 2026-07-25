"""Artifact-keyed records for declared outputs that never landed (issue #2559).

``Task.declared_outputs`` states what a task intends to leave behind. When the
task produces it, the spine holds a production entry under that artifact key and
every artifact-side surface can answer for it. When the task *doesn't*, the
artifact side holds nothing at all -- and "a task tried and failed" is then
indistinguishable from "nothing was ever scheduled to produce this". Both read as
``no spine entry records this artifact``.

That gap is the difference between an operator running one command and an
operator correlating run directories by hand, and it is exactly the sort of
question the URI-keyed chain exists to answer. So the absence is recorded on the
same chain as the presence:

    an **attempt record** is a spine entry keyed by the *declared* artifact URI,
    whose ``step_id`` carries :data:`~bernstein.core.lineage.spine.ARTIFACT_ATTEMPT_STEP_PREFIX`,
    the outcome and the declaring task id, and whose ``content_hash`` covers a
    canonical description of the attempt rather than any artifact bytes.

Because it is an ordinary entry it inherits the whole substrate for free: it is
Merkle-chained to its predecessor, HMAC-tagged, replayed by
:func:`~bernstein.core.lineage.artifact_events.replay_production_events`, and
tamper-evident per entry. A record saying "task X tried and did not produce this"
is therefore as verifiable, and as hard to forge after the fact, as a record
saying it did.

What an attempt is *not*
------------------------

It is not a production, and nothing may treat it as one. Three consumers filter
it out explicitly, each for a reason worth stating:

* :func:`~bernstein.core.lineage.artifact_health.collect_artifact_state` keeps
  attempts in their own bucket, so ``production_count`` counts productions and
  the tip is never an attempt.
* :func:`~bernstein.core.lineage.artifact_events.observed_artifact_keys` skips
  them -- otherwise the record of a *missing* output would satisfy its own
  declaration on the next reconciliation, and the finding would erase itself.
* :func:`~bernstein.core.trigger_sources.artifact.intended_fires` skips them:
  downstream goals react to an output landing, never to one failing to.

Determinism
-----------

:func:`attempt_record_bytes` is a pure function of the task id, the artifact key,
the outcome and the reason. No clock, no host, no run id. Two reconciliations of
the same declaration produce byte-identical content hashes, which is what lets a
fixture replay reproduce the chain exactly.

Fail-open
---------

:func:`record_output_attempt` is strict -- it is the write. The reconciliation
that drives it, :func:`reconcile_declared_outputs`, **never raises**: it runs on
the task-completion path, where a task that already finished must never be
failed by the bookkeeping that describes it. A dropped attempt record costs a
question that stays unanswered; a raised exception would cost the completion
itself.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

from bernstein.core.lineage.artifact_events import emit_production_event
from bernstein.core.lineage.artifact_uri import (
    ArtifactURIError,
    canonical_artifact_key,
    is_glob_pattern,
)
from bernstein.core.lineage.spine import ARTIFACT_ATTEMPT_STEP_PREFIX, LineageSpine

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from bernstein.core.lineage.spine import SpineEntry

logger = logging.getLogger(__name__)

__all__ = [
    "ARTIFACT_ATTEMPT_STEP_PREFIX",
    "ATTEMPT_OUTCOMES",
    "ATTEMPT_OUTCOME_FAILED",
    "ATTEMPT_OUTCOME_INCOMPLETE",
    "ATTEMPT_RECORD_VERSION",
    "attempt_outcome",
    "attempt_record_bytes",
    "attempt_step_id",
    "attempt_task_id",
    "is_attempt_step_id",
    "reconcile_declared_outputs",
    "record_output_attempt",
]

#: Wire-format version of the attempt payload. Bump only on a shape change; the
#: version is inside the hashed bytes, so an old record stays verifiable under
#: the hash it was written with.
ATTEMPT_RECORD_VERSION = 1

#: The task did not reach a successful completion.
ATTEMPT_OUTCOME_FAILED = "failed"

#: The task completed, but a declared output is not on the chain. A quieter
#: finding than a failure and a more interesting one: the work was accepted while
#: the thing it promised to leave behind is absent.
ATTEMPT_OUTCOME_INCOMPLETE = "incomplete"

#: Every outcome an attempt record may carry.
ATTEMPT_OUTCOMES = (ATTEMPT_OUTCOME_FAILED, ATTEMPT_OUTCOME_INCOMPLETE)

#: Reasons are operator- and agent-supplied text that lands inside hashed bytes.
#: Bounded so a runaway message cannot bloat the chain.
_MAX_REASON_CHARS = 500


def attempt_step_id(*, outcome: str, task_id: str) -> str:
    """Return the ``step_id`` an attempt record is written under.

    The shape is ``artifact-attempt:<outcome>:<task_id>``. Both facts live in the
    ``step_id`` rather than only inside the hashed payload because ``step_id`` is
    a field of the entry itself: a reader holding a spine row can say who
    declared the output and how the attempt ended without fetching anything else,
    and both facts are covered by the entry hash and the HMAC tag, so neither can
    be edited after the fact without breaking verification.
    """
    return f"{ARTIFACT_ATTEMPT_STEP_PREFIX}{outcome}:{task_id}"


def is_attempt_step_id(step_id: str) -> bool:
    """Whether ``step_id`` marks its entry as an attempt record."""
    return step_id.startswith(ARTIFACT_ATTEMPT_STEP_PREFIX)


def _attempt_parts(step_id: str) -> tuple[str, str]:
    """Split an attempt ``step_id`` into ``(outcome, task_id)``.

    Returns ``("", "")`` for anything that is not a well-formed attempt marker,
    so callers projecting a mixed entry stream need no branch.
    """
    if not is_attempt_step_id(step_id):
        return "", ""
    rest = step_id[len(ARTIFACT_ATTEMPT_STEP_PREFIX) :]
    outcome, sep, task_id = rest.partition(":")
    if not sep:
        return "", rest
    return outcome, task_id


def attempt_task_id(step_id: str) -> str:
    """Return the declaring task id carried by an attempt ``step_id``."""
    return _attempt_parts(step_id)[1]


def attempt_outcome(step_id: str) -> str:
    """Return the outcome carried by an attempt ``step_id``."""
    return _attempt_parts(step_id)[0]


def attempt_record_bytes(*, task_id: str, uri: str, outcome: str, reason: str = "") -> bytes:
    """Return the canonical bytes an attempt entry's ``content_hash`` covers.

    Pure and clock-free: the same declaration always hashes to the same value, so
    the record is reproducible by anyone holding the same facts and a replayed
    fixture reproduces the chain byte-for-byte.

    The bytes are self-describing on purpose. A verifier reading an attempt entry
    off the spine recomputes these bytes from the fields the entry already
    carries and checks the hash, rather than trusting a side table to say what
    the entry meant.

    Args:
        task_id: The task that declared the output.
        uri: The canonical declared artifact key.
        outcome: One of :data:`ATTEMPT_OUTCOMES`.
        reason: Optional short explanation, truncated to a bounded length.

    Returns:
        Canonical JSON bytes: sorted keys, minimal separators.
    """
    payload = {
        "outcome": outcome,
        "reason": reason[:_MAX_REASON_CHARS],
        "task_id": task_id,
        "uri": uri,
        "v": ATTEMPT_RECORD_VERSION,
    }
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")


def record_output_attempt(
    lineage_root: Path,
    *,
    run_id: str,
    uri: str,
    task_id: str,
    actor: str,
    model: str,
    hmac_key: bytes,
    timestamp: int,
    outcome: str = ATTEMPT_OUTCOME_FAILED,
    reason: str = "",
) -> SpineEntry:
    """Append one attempt record for ``uri`` and return the entry.

    Strict: this is the write, and a provenance write that cannot land is an
    error the caller must see. The fail-open wrapper is
    :func:`reconcile_declared_outputs`.

    Args:
        lineage_root: ``.sdd/lineage`` root.
        run_id: Run whose spine the record joins.
        uri: The declared artifact key. Canonicalised before use.
        task_id: The declaring task; becomes the ``step_id`` suffix.
        actor: Identity that ran the task.
        model: Model the task ran under.
        hmac_key: Audit-chain key the spine tags entries with.
        timestamp: Integer timestamp in the unit this spine uses.
        outcome: One of :data:`ATTEMPT_OUTCOMES`.
        reason: Optional short explanation.

    Returns:
        The materialised entry.

    Raises:
        ValueError: When ``outcome`` is unknown, or when the spine boundary
            refuses the key (absolute path, traversal, unknown scheme).
        bernstein.core.lineage.artifact_uri.ArtifactURIError: When ``uri`` is not
            a valid artifact key.
    """
    if outcome not in ATTEMPT_OUTCOMES:
        msg = f"unknown attempt outcome {outcome!r}; expected one of {ATTEMPT_OUTCOMES}"
        raise ValueError(msg)
    key = canonical_artifact_key(uri)
    entry = LineageSpine(lineage_root, run_id=run_id, hmac_key=hmac_key).record_entry(
        artifact_path=key,
        content=attempt_record_bytes(task_id=task_id, uri=key, outcome=outcome, reason=reason),
        actor=actor,
        step_id=attempt_step_id(outcome=outcome, task_id=task_id),
        model=model,
        timestamp=timestamp,
    )
    # Journal the entry like any other so the fan-out stays exact: replaying the
    # spine must reproduce the journal row for row, and an entry that skipped the
    # journal would replay as a dropped firing (issue #2559).
    #
    # Journaled but deliberately *not* published. The live event name is
    # ``artifact.produced``, and this entry records the opposite; putting it on
    # the bus would tell every subscriber an artifact landed when it did not.
    # Consumers that want attempts read them from the chain, where they carry
    # their proof with them.
    emit_production_event(lineage_root, run_id=run_id, entry=entry)
    return entry


def reconcile_declared_outputs(
    lineage_root: Path,
    *,
    run_id: str,
    declared: Sequence[str],
    task_id: str,
    actor: str,
    model: str,
    hmac_key: bytes,
    timestamp: int,
    outcome: str = ATTEMPT_OUTCOME_FAILED,
    reason: str = "",
) -> tuple[str, ...]:
    """Record an attempt for every declared output this run did not produce.

    The question "did the declared artifact land?" is answered **per URI against
    this run's spine**, which needs no task-level attribution of writes: the
    chain is already keyed by artifact, so the lookup is exact. A production
    recorded by an *earlier* run does not count -- the declaration was made by
    this task, about this run, and a stale artifact from last week is not this
    task's output.

    Glob patterns are skipped. ``pkg://pypi/bernstein/*`` names a set, not an
    artifact, so there is no single key to record the attempt under; a
    declaration that wants an attempt record has to name the thing it promises.

    **Never raises.** Called from the completion path, where the task has already
    finished and nothing about describing it may change that outcome.

    Args:
        lineage_root: ``.sdd/lineage`` root.
        run_id: Run whose spine is consulted and appended to.
        declared: The task's declared outputs, keys or patterns.
        task_id: The declaring task.
        actor: Identity that ran the task.
        model: Model the task ran under.
        hmac_key: Audit-chain key.
        timestamp: Integer timestamp for any record written.
        outcome: One of :data:`ATTEMPT_OUTCOMES`.
        reason: Optional short explanation.

    Returns:
        The artifact keys an attempt was recorded for, sorted. Empty when every
        declared output landed, when nothing concrete was declared, or when the
        reconciliation could not run at all.
    """
    if not declared:
        return ()

    try:
        keys = _concrete_keys(declared)
        if not keys:
            return ()
        produced, attempted = _run_index(lineage_root, run_id=run_id, hmac_key=hmac_key)
    except Exception as exc:  # fail-open: bookkeeping must not fail a finished task
        logger.debug("artifact attempt: reconciliation could not read run %s: %s", run_id, exc)
        return ()

    recorded: list[str] = []
    for key in keys:
        if key in produced:
            continue
        # Idempotent per (task, uri): a reap that runs twice must not stack
        # duplicate findings on the chain.
        if (task_id, key) in attempted:
            continue
        try:
            record_output_attempt(
                lineage_root,
                run_id=run_id,
                uri=key,
                task_id=task_id,
                actor=actor,
                model=model,
                hmac_key=hmac_key,
                timestamp=timestamp,
                outcome=outcome,
                reason=reason,
            )
        except Exception as exc:  # fail-open, per key: one bad key does not stop the rest
            logger.debug("artifact attempt: could not record attempt for %s: %s", key, exc)
            continue
        recorded.append(key)
    return tuple(recorded)


def _concrete_keys(declared: Sequence[str]) -> tuple[str, ...]:
    """Return the canonical, non-glob artifact keys inside ``declared``, sorted.

    Anything that does not canonicalise to a concrete key -- a glob, an absolute
    path, a traversal, an unknown scheme -- is dropped. Operator input reaches
    this path, and a malformed declaration is a finding for the task spec, not a
    reason to abandon the whole reconciliation.
    """
    out: set[str] = set()
    for raw in declared:
        # A repo-relative key accepts ``*`` and ``?`` as ordinary filename
        # characters, so the glob check cannot be left to the parser: it has to
        # be made explicitly against the raw declaration.
        if isinstance(raw, str) and is_glob_pattern(raw):
            logger.debug("artifact attempt: skipping declared pattern %r; it names a set, not an artifact", raw)
            continue
        try:
            out.add(canonical_artifact_key(raw))
        except (ArtifactURIError, TypeError):
            logger.debug("artifact attempt: skipping declared output %r; not a concrete artifact key", raw)
    return tuple(sorted(out))


def _run_index(
    lineage_root: Path,
    *,
    run_id: str,
    hmac_key: bytes,
) -> tuple[frozenset[str], frozenset[tuple[str, str]]]:
    """Return ``(produced keys, (task_id, key) pairs already attempted)`` for a run.

    One pass over the run's spine feeds both the "did it land?" question and the
    idempotency check, so reconciliation reads the chain once regardless of how
    many outputs a task declared.
    """
    produced: set[str] = set()
    attempted: set[tuple[str, str]] = set()
    try:
        spine = LineageSpine(lineage_root, run_id=run_id, hmac_key=hmac_key)
    except ValueError:
        # An unusable run dir carries no productions; every declared output is
        # then unattempted, which is the honest answer.
        return frozenset(), frozenset()
    for entry in spine.iter_entries():
        if is_attempt_step_id(entry.step_id):
            attempted.add((attempt_task_id(entry.step_id), entry.artifact_path))
        else:
            produced.add(entry.artifact_path)
    return frozenset(produced), frozenset(attempted)
