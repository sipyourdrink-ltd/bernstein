"""Deterministic replay package for Bernstein agent runs.

This package provides the *gateway* that intercepts LLM requests and
tool dispatches so a previously recorded run can be re-executed against
recorded fixtures instead of live providers.

Public surface:

* :class:`EventJournal` - the single always-on Merkle-chained per-run
  event recorder whose head hash is the run identity (issue #2293).
* :class:`ReplayGateway` - fixture-replay adapter around LLM + tool calls.
* :data:`RECORD_ENV_VAR` - env-var that opts the *gateway* into recording.
* :func:`diff_event_logs` - line-by-line first-divergence locator.

The :class:`EventJournal` is the canonical run recorder: it records by
default into ``.sdd/runs/<run_id>/journal.jsonl`` and its head hash is the
run identity. It replaced the old orchestrator ``RunRecorder``.

The :class:`ReplayGateway` is a distinct concern - it captures LLM/tool
I/O so a run can be re-executed against recorded fixtures. It is OFF by
default; set ``BERNSTEIN_RECORD=1`` or pass ``record=True`` to enable it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from bernstein.core.replay.diff import (
    DivergenceResult,
    diff_event_logs,
    load_events,
)
from bernstein.core.replay.fork import (
    ForkError,
    ForkResult,
    fork_run,
    record_snapshot_event,
)
from bernstein.core.replay.gateway import (
    EVENTS_FILENAME,
    RECORD_ENV_VAR,
    GatewayMode,
    ReplayGateway,
    ReplayMissError,
    is_recording_enabled,
)
from bernstein.core.replay.journal import (
    JOURNAL_FILENAME,
    RETENTION_ENV_VAR,
    EventJournal,
    JournalVerifyResult,
    rebuild_state,
    seal_journal_into_spine,
    verify_journal,
)
from bernstein.core.replay.provider_state import (
    CAPABILITY_DECLARED_BLIND,
    CAPABILITY_OBSERVED,
    MUTATION_CAPABILITY_EVENT,
    PROVIDER_STATE_CAPTURE_FAILED_EVENT,
    PROVIDER_STATE_MUTATION_EVENT,
    ProviderStateMutation,
    ProviderStateVerifyResult,
    record_agent_mutations,
    record_capture_failure,
    record_mutation_capability,
    record_provider_state_mutation,
    verify_provider_state,
)

if TYPE_CHECKING:
    from pathlib import Path

    from bernstein.adapters.session_id import SessionIdRecord


def locate_run(sdd_dir: Path, conversation_id: str, adapter_name: str) -> SessionIdRecord | None:
    """Locate a previously recorded run by ``(conversation_id, adapter_name)``.

    Resolves the run directly from the deterministic session-id index under
    ``<sdd_dir>/session_index.json`` without scanning any ``events.jsonl``
    logs (AC #4 of deterministic session-id binding). Returns ``None`` when
    the pair was never recorded.
    """
    from bernstein.adapters.session_id import SessionIdIndex

    return SessionIdIndex(sdd_dir).lookup(conversation_id, adapter_name)


def record_run(sdd_dir: Path, conversation_id: str, adapter_name: str, run_id: str) -> SessionIdRecord:
    """Bind ``(conversation_id, adapter_name)`` to ``run_id`` for later replay.

    Writes the deterministic session-id index entry that :func:`locate_run`
    reads back. The latest binding for a key wins, so a rerun overwrites the
    prior slot rather than appending a duplicate.
    """
    from bernstein.adapters.session_id import SessionIdIndex

    return SessionIdIndex(sdd_dir).record(conversation_id, adapter_name, run_id)


__all__ = [
    "CAPABILITY_DECLARED_BLIND",
    "CAPABILITY_OBSERVED",
    "EVENTS_FILENAME",
    "JOURNAL_FILENAME",
    "MUTATION_CAPABILITY_EVENT",
    "PROVIDER_STATE_CAPTURE_FAILED_EVENT",
    "PROVIDER_STATE_MUTATION_EVENT",
    "RECORD_ENV_VAR",
    "RETENTION_ENV_VAR",
    "DivergenceResult",
    "EventJournal",
    "ForkError",
    "ForkResult",
    "GatewayMode",
    "JournalVerifyResult",
    "ProviderStateMutation",
    "ProviderStateVerifyResult",
    "ReplayGateway",
    "ReplayMissError",
    "diff_event_logs",
    "fork_run",
    "is_recording_enabled",
    "load_events",
    "locate_run",
    "rebuild_state",
    "record_agent_mutations",
    "record_capture_failure",
    "record_mutation_capability",
    "record_provider_state_mutation",
    "record_run",
    "record_snapshot_event",
    "seal_journal_into_spine",
    "verify_journal",
    "verify_provider_state",
]
