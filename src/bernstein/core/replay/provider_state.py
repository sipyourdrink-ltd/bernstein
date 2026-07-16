"""Provider-side context mutation records for the replay journal.

Issue #2507. Deterministic replay assumes context-as-sent equals
context-as-consumed. The client side of that assumption is defended by
the compaction policy recorded in the step fingerprint (see
``bernstein.adapters.claude.apply_compaction_policy``), but providers
also mutate model context server-side: compaction and similar opaque
state carried between calls. The :class:`~bernstein.core.replay.journal.EventJournal`
chains what was sent and what came back; a server-side rewrite between
those two points was previously invisible, so replay either diverged
with no attributable cause or matched byte-for-byte while the recorded
run was not what the model actually consumed.

This module treats provider-side state mutations as first-class chain
entries:

* Every observed mutation signal is recorded as a content-addressed
  :data:`PROVIDER_STATE_MUTATION_EVENT` journal entry hashed over
  ``(kind, before_digest, after_digest, step_index)``. Because the
  journal chains every entry, removing or editing a mutation record
  breaks chain verification at exactly that index: the record is
  load-bearing, not a side log.
* In deterministic-record and hermetic-replay modes
  (:data:`DETERMINISTIC_SEED_ENV` / :data:`REPLAY_RUN_ID_ENV`) mutation
  features are requested off at spawn time; a mutation that still
  arrives is recorded **flagged** and :func:`verify_provider_state`
  fails closed instead of silently accepting it. Outside deterministic
  mode mutations are permitted but pinned: each is recorded before
  execution continues.
* An adapter's ability to observe mutations at all is itself recorded
  per run as a :data:`MUTATION_CAPABILITY_EVENT` entry, so an absence
  of mutation entries is distinguishable from an inability to see them.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping
    from pathlib import Path

    from bernstein.core.replay.journal import EventJournal

logger = logging.getLogger(__name__)

#: Journal event type for one observed provider-side context mutation.
PROVIDER_STATE_MUTATION_EVENT = "provider_state_mutation"

#: Journal event type recording an adapter's mutation-observability
#: capability for the run ("observed" or "declared-blind").
MUTATION_CAPABILITY_EVENT = "provider_state_capability"

#: The adapter surfaces provider-side mutation signals from its stream.
CAPABILITY_OBSERVED = "observed"

#: The adapter has no observation surface for provider-side mutations.
#: Recorded per run so missing mutation entries stay interpretable.
CAPABILITY_DECLARED_BLIND = "declared-blind"

#: Env var naming deterministic-record mode. Mirrors the flag read by the
#: orchestrator and the claude adapter; duplicated here so the replay
#: layer does not import scheduler internals.
DETERMINISTIC_SEED_ENV = "BERNSTEIN_DETERMINISTIC_SEED"

#: Env var naming hermetic-replay mode (see :data:`DETERMINISTIC_SEED_ENV`).
REPLAY_RUN_ID_ENV = "BERNSTEIN_REPLAY_RUN_ID"

#: Prefixes that mark provider-reported *pre-mutation* state fields inside
#: a mutation signal's detail payload (for example ``pre_tokens``).
_BEFORE_FIELD_PREFIXES = ("pre_", "before_")


def deterministic_mode_active(env: Mapping[str, str] | None = None) -> bool:
    """Return whether this process runs in deterministic-record or replay mode.

    Args:
        env: Optional environment mapping (defaults to ``os.environ``).

    Returns:
        ``True`` when a deterministic seed or a replay run id is present.
    """
    src = os.environ if env is None else env
    seed = src.get(DETERMINISTIC_SEED_ENV, "").strip()
    replay = src.get(REPLAY_RUN_ID_ENV, "").strip()
    return bool(seed) or bool(replay)


def _canonical_sha256(payload: Any) -> str:
    """Return the SHA-256 hex digest of the canonical JSON of *payload*."""
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


@dataclass(frozen=True, slots=True)
class ProviderStateMutation:
    """One provider-side context mutation observed during a run.

    Attributes:
        kind: Mutation kind as reported by the provider stream (for
            example ``compact_boundary``).
        before_digest: Content digest of the provider-reported
            pre-mutation state metadata (empty-object digest when the
            provider reported none).
        after_digest: Content digest of the full provider-reported
            mutation payload.
        step_index: 0-based position of the mutation within the
            session's observed signal order.
        flagged: ``True`` when the mutation arrived in deterministic
            mode despite suppression being requested. Policy state, not
            mutation identity: excluded from :meth:`content_address`.
    """

    kind: str
    before_digest: str
    after_digest: str
    step_index: int
    flagged: bool = False

    def content_address(self) -> str:
        """Return the content address hashed over the identity fields.

        The address is ``SHA-256`` of the canonical JSON of
        ``(kind, before_digest, after_digest, step_index)``, so two
        independent recorders derive the same address for the same
        reported mutation.
        """
        return _canonical_sha256(
            {
                "kind": self.kind,
                "before_digest": self.before_digest,
                "after_digest": self.after_digest,
                "step_index": self.step_index,
            }
        )


def mutation_from_signal(
    kind: str,
    detail: Mapping[str, Any] | None,
    *,
    step_index: int,
    flagged: bool = False,
) -> ProviderStateMutation:
    """Build a :class:`ProviderStateMutation` from a raw stream signal.

    The provider-reported metadata is the only observable surface for a
    server-side rewrite, so the digests are content addresses of that
    report: ``before_digest`` covers the fields the provider labelled as
    pre-mutation state (``pre_*`` / ``before_*``), ``after_digest``
    covers the full reported payload.

    Args:
        kind: Mutation kind from the signal (for example
            ``compact_boundary``).
        detail: Raw provider-reported payload for the signal.
        step_index: 0-based position within the session's signal order.
        flagged: Whether the mutation arrived in deterministic mode.

    Returns:
        The content-addressable mutation record.
    """
    payload: dict[str, Any] = dict(detail or {})
    before_fields = {k: v for k, v in payload.items() if k.startswith(_BEFORE_FIELD_PREFIXES)}
    return ProviderStateMutation(
        kind=str(kind),
        before_digest=_canonical_sha256(before_fields),
        after_digest=_canonical_sha256(payload),
        step_index=step_index,
        flagged=flagged,
    )


def record_provider_state_mutation(
    journal: EventJournal,
    mutation: ProviderStateMutation,
    **extra: Any,
) -> None:
    """Chain one provider-side mutation into the run journal.

    The entry participates in the journal's Merkle chain like any other
    event: the head after this call commits to the mutation, so a later
    removal or edit of the entry breaks verification at exactly its
    index.

    Args:
        journal: The run's event journal.
        mutation: The mutation record to chain.
        **extra: Additional attribution fields (for example
            ``agent_id``) folded into the hashed payload.
    """
    journal.record(
        PROVIDER_STATE_MUTATION_EVENT,
        mutation_kind=mutation.kind,
        before_digest=mutation.before_digest,
        after_digest=mutation.after_digest,
        step_index=mutation.step_index,
        content_address=mutation.content_address(),
        flagged=mutation.flagged,
        **extra,
    )


def record_mutation_capability(journal: EventJournal, *, adapter: str, capability: str) -> None:
    """Record an adapter's mutation-observability capability for the run.

    Args:
        journal: The run's event journal.
        adapter: Adapter registry name (or provider label when the
            adapter could not be resolved).
        capability: :data:`CAPABILITY_OBSERVED` or
            :data:`CAPABILITY_DECLARED_BLIND`.
    """
    journal.record(MUTATION_CAPABILITY_EVENT, adapter=adapter, capability=capability)


def capability_for_provider(provider: str | None, model: str = "") -> tuple[str, str]:
    """Resolve the mutation-observability capability for a provider.

    Falls back to :data:`CAPABILITY_DECLARED_BLIND` whenever the adapter
    cannot be resolved: an unresolvable observation surface must read as
    an inability to see mutations, never as their absence.

    Args:
        provider: Provider label from the agent session (may be ``None``).
        model: Model string used for provider-name inference.

    Returns:
        ``(adapter_name, capability)``.
    """
    label = (provider or "").strip()
    try:
        from bernstein.adapters.registry import adapter_name_for_provider, get_adapter

        name = adapter_name_for_provider(provider, model) or label
        if not name:
            return "unknown", CAPABILITY_DECLARED_BLIND
        adapter = get_adapter(name)
        capability = str(
            getattr(adapter, "provider_mutation_observability", CAPABILITY_DECLARED_BLIND),
        )
        return name, capability
    except Exception:
        return label or "unknown", CAPABILITY_DECLARED_BLIND


def record_agent_mutations(
    journal: EventJournal,
    signals: Iterable[Mapping[str, Any]],
    *,
    agent_id: str,
    deterministic: bool | None = None,
    audit_chain: Any | None = None,
) -> int:
    """Chain every observed mutation signal for one agent session.

    In deterministic mode each entry is flagged (suppression was
    requested, the mutation arrived anyway) so
    :func:`verify_provider_state` fails closed; otherwise the entries
    pin the mutations as part of the run identity. When *audit_chain*
    is supplied, each chained entry is additionally mirrored into the
    HMAC audit chain as a ``provider.state_mutation`` event anchored to
    the journal head after the entry was chained.

    Args:
        journal: The run's event journal.
        signals: Observed signals, each ``{"kind": str, "detail": dict}``,
            in session observation order.
        agent_id: The agent session the signals were observed for.
        deterministic: Override for the run mode; defaults to
            :func:`deterministic_mode_active`.
        audit_chain: Optional
            :class:`~bernstein.core.security.audit_chain.AuditChainStore`
            accepting the mirrored events. Mirroring is best-effort: a
            chain failure never blocks the journal entry.

    Returns:
        The number of mutation entries chained.
    """
    flagged = deterministic_mode_active() if deterministic is None else deterministic
    recorded = 0
    for step_index, signal in enumerate(signals):
        kind = str(signal.get("kind", "")) or "unknown"
        detail = signal.get("detail")
        mutation = mutation_from_signal(
            kind,
            detail if isinstance(detail, dict) else {},
            step_index=step_index,
            flagged=flagged,
        )
        record_provider_state_mutation(journal, mutation, agent_id=agent_id)
        recorded += 1
        if audit_chain is not None:
            _mirror_to_audit_chain(audit_chain, journal, mutation, agent_id=agent_id)
    if recorded and flagged:
        logger.warning(
            "provider-state: %d mutation(s) arrived for agent %s in deterministic mode; recorded flagged (fail-closed)",
            recorded,
            agent_id,
        )
    return recorded


def _mirror_to_audit_chain(
    audit_chain: Any,
    journal: EventJournal,
    mutation: ProviderStateMutation,
    *,
    agent_id: str,
) -> None:
    """Mirror one chained mutation into the HMAC audit chain (best-effort)."""
    try:
        from bernstein.core.security.audit_chain import record_provider_state_mutation as _record_audit

        _record_audit(
            chain=audit_chain,
            run_id=journal.run_id,
            agent_id=agent_id,
            mutation_kind=mutation.kind,
            content_address=mutation.content_address(),
            step_index=mutation.step_index,
            flagged=mutation.flagged,
            journal_head=journal.head(),
        )
    except Exception as exc:
        # The journal entry is the load-bearing record; the audit mirror is
        # provenance decoration and must never block it. Only the exception
        # type is logged: this path touches the audit HMAC key.
        logger.warning("provider-state: audit mirror failed: %s", type(exc).__name__)


@dataclass(frozen=True, slots=True)
class ProviderStateVerifyResult:
    """Outcome of :func:`verify_provider_state`.

    Attributes:
        ok: ``True`` only when the chain recomputes cleanly, every
            mutation entry's content address matches its fields, and no
            entry is flagged.
        chain_ok: Whether the underlying Merkle chain verified.
        mutation_count: Number of mutation entries walked.
        flagged_indices: Journal indices of flagged mutation entries
            (mutations that arrived in deterministic mode).
        errors: Human-readable explanations for every failure.
    """

    ok: bool
    chain_ok: bool
    mutation_count: int
    flagged_indices: list[int] = field(default_factory=list[int])
    errors: list[str] = field(default_factory=list[str])


def verify_provider_state(path: Path) -> ProviderStateVerifyResult:
    """Verify a journal's provider-state mutation entries.

    Extends plain chain verification with the fail-closed mutation
    policy: a flagged entry (a mutation that arrived in deterministic
    mode) or a mutation entry whose stored content address does not
    match its recorded fields makes the result not ``ok`` even when the
    chain itself is intact. Journals without mutation entries verify
    exactly as before.

    Args:
        path: Path to a ``journal.jsonl`` file.

    Returns:
        A :class:`ProviderStateVerifyResult`.
    """
    from bernstein.core.replay.journal import load_events, verify_journal

    chain = verify_journal(path)
    errors = list(chain.errors)
    flagged: list[int] = []
    mutation_count = 0

    for index, row in enumerate(load_events(path)):
        if str(row.get("event", "")) != PROVIDER_STATE_MUTATION_EVENT:
            continue
        mutation_count += 1
        kind = str(row.get("mutation_kind", ""))
        expected_address = ProviderStateMutation(
            kind=kind,
            before_digest=str(row.get("before_digest", "")),
            after_digest=str(row.get("after_digest", "")),
            step_index=int(row.get("step_index", 0)),
        ).content_address()
        if str(row.get("content_address", "")) != expected_address:
            errors.append(f"step {index}: provider_state_mutation content address mismatch")
        if bool(row.get("flagged", False)):
            flagged.append(index)
            errors.append(
                f"step {index}: flagged provider-side mutation ({kind}) arrived in deterministic mode",
            )

    return ProviderStateVerifyResult(
        ok=chain.ok and not errors,
        chain_ok=chain.ok,
        mutation_count=mutation_count,
        flagged_indices=flagged,
        errors=errors,
    )


__all__ = [
    "CAPABILITY_DECLARED_BLIND",
    "CAPABILITY_OBSERVED",
    "DETERMINISTIC_SEED_ENV",
    "MUTATION_CAPABILITY_EVENT",
    "PROVIDER_STATE_MUTATION_EVENT",
    "REPLAY_RUN_ID_ENV",
    "ProviderStateMutation",
    "ProviderStateVerifyResult",
    "capability_for_provider",
    "deterministic_mode_active",
    "mutation_from_signal",
    "record_agent_mutations",
    "record_mutation_capability",
    "record_provider_state_mutation",
    "verify_provider_state",
]
