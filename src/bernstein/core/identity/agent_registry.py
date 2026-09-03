"""Agent-principal registry projected from the grant and delegation chains (#4969).

Bernstein already treats agents as principals: ``bernstein spiffe id`` derives a
deterministic identity per install and agent, :mod:`bernstein.core.identity.grants`
issues Ed25519-signed, chain-anchored per-task credential grants, and
:mod:`bernstein.core.identity.delegation` records which principal authorized
which sub-agent action. What was missing is the noun -- an object an operator
can list, describe, or hand to an auditor that answers *which agents exist
here, what may each one do, and who decided that*.

This module supplies that noun as a **projection**, never a second source of
truth. :func:`project_agents` folds the verified grant and delegation chains
under one audit root into :class:`AgentPrincipal` entries; :func:`render_registry`
renders the fold as canonical JSON; :func:`verify_registry` recomputes the fold
and refuses a stored projection that names an agent the chain does not
establish. Delete the rendered projection and rebuild it from the same chain at
the same ``as_of`` instant and the bytes are identical -- a registry that is its
own authority drifts from what happened; a fold over the chain cannot.

What establishes a principal
----------------------------
A principal is any non-empty party a **tamper-verified** chain event names in an
authority-bearing role: ``issuer`` / ``subject`` / ``audience`` on a delegation
receipt, ``issuer`` / ``audience`` on a grant record. A chain whose HMAC linkage
does not hold establishes nothing at all -- its runs contribute no principals
and its failure is reported in :attr:`AgentRegistryProjection.errors`, so a
writer without the install audit key cannot mint an agent by appending a line.

What "the ceiling currently in force" means
-------------------------------------------
The ceiling is recomputed at the projection's ``as_of`` instant, not read off
the grant that was widest at issue time. A grant contributes only while it is
issued, unrevoked, and unexpired; a delegation scope contributes only while its
``not_after`` has not passed. Both vocabularies are projected onto the
``PERM_*`` set through :func:`~bernstein.core.security.capability_tokens.scope_permissions`,
so a coarse ``read``/``write``/``execute``/``full`` ceiling recorded on a grant
and an explicit permission set recorded on a delegation scope are comparable
rather than re-derived here. Delegation scopes contribute only from runs whose
recomputed authority checks also passed: a hop that widened its parent is a real
event (so its parties are listed) but is not evidence of authority in force.

An agent with nothing in force is listed with an empty ceiling rather than
dropped -- an agent that exists and may currently do nothing is exactly what an
auditor needs to see.

This slice reads. Nothing here writes to either chain.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, cast, get_args

from bernstein.core.identity import delegation, grants
from bernstein.core.identity.delegation_scope import DelegationScope
from bernstein.core.identity.spiffe.spiffe_id import (
    SpiffeIdError,
    derive_spiffe_id_from_key,
    parse_spiffe_id,
    validate_path_segment,
)
from bernstein.core.security.capability_tokens import scope_permissions
from bernstein.core.security.permission_delegation import DelegationScope as _CoarseScope

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Iterable

__all__ = [
    "REGISTRY_VERSION",
    "SOURCE_DELEGATION",
    "SOURCE_GRANT",
    "AgentPrincipal",
    "AgentRegistryProjection",
    "ChainEventRef",
    "RegistryError",
    "RegistryVerification",
    "project_agents",
    "render_registry",
    "verify_registry",
]

#: Schema version carried in the rendered projection so a stored file that was
#: produced by an older fold is recognisable rather than silently mis-compared.
REGISTRY_VERSION: Final[int] = 1

SOURCE_GRANT: Final[str] = "grant"
SOURCE_DELEGATION: Final[str] = "delegation"

#: The coarse scope vocabulary grant records use for ``capability_ceiling``.
#: Read off the ``DelegationScope`` literal in
#: :mod:`bernstein.core.security.permission_delegation` rather than restated,
#: so the two surfaces cannot drift apart.
_COARSE_SCOPES: Final[frozenset[str]] = frozenset(get_args(_CoarseScope))

_ROLE_ISSUER: Final[str] = "issuer"
_ROLE_SUBJECT: Final[str] = "subject"
_ROLE_AUDIENCE: Final[str] = "audience"


class RegistryError(ValueError):
    """Raised when a registry entry is not established by any chain event."""


@dataclass(frozen=True)
class ChainEventRef:
    """A pointer to the one chain event that named a principal in one role.

    Attributes:
        source: :data:`SOURCE_GRANT` or :data:`SOURCE_DELEGATION`.
        run_id: Run whose ledger file carries the event.
        index: ``record_index`` (grant) or ``hop_index`` (delegation).
        kind: Grant record kind, or the delegated ``act`` for a delegation hop.
        role: Which field of the event named the principal.
        anchor: The event's HMAC -- the chain position the entry rests on.
    """

    source: str
    run_id: str
    index: int
    kind: str
    role: str
    anchor: str

    def to_body(self) -> dict[str, Any]:
        """Return the canonical dict rendered into the projection."""
        return {
            "anchor": self.anchor,
            "index": self.index,
            "kind": self.kind,
            "role": self.role,
            "run_id": self.run_id,
            "source": self.source,
        }

    def sort_key(self) -> tuple[str, str, int, str]:
        """Return the deterministic ordering key for rendering."""
        return (self.source, self.run_id, self.index, self.role)


@dataclass(frozen=True)
class AgentPrincipal:
    """One agent principal, with the chain events that established it.

    Attributes:
        agent_id: The principal name exactly as the chain recorded it.
        chain_events: Every event that named this principal (never empty).
        spiffe_id: The derived SPIFFE ID, or ``None`` when no id is derivable
            without coining one (see :func:`project_agents`).
        capability_ceiling: The ``PERM_*`` ceiling in force at the projection's
            ``as_of`` instant.
        grants: Ids of grants issued to this principal, oldest first.
        delegations: ``<run>#<hop>`` refs of the delegation hops it issued.
    """

    agent_id: str
    chain_events: tuple[ChainEventRef, ...]
    spiffe_id: str | None = None
    capability_ceiling: tuple[str, ...] = ()
    grants: tuple[str, ...] = ()
    delegations: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        """Refuse an entry no chain event establishes.

        The registry is a fold, so an entry with nothing behind it is not an
        incomplete record -- it is an invented one, and inventing is the single
        failure mode a projection must not have.
        """
        if not self.agent_id:
            raise RegistryError("agent principal must have a non-empty id")
        if not self.chain_events:
            raise RegistryError(f"agent {self.agent_id!r} is not established by any chain event")

    def to_body(self) -> dict[str, Any]:
        """Return the canonical dict rendered into the projection."""
        return {
            "agent_id": self.agent_id,
            "capability_ceiling": list(self.capability_ceiling),
            "chain_events": [e.to_body() for e in self.chain_events],
            "delegations": list(self.delegations),
            "grants": list(self.grants),
            "spiffe_id": self.spiffe_id,
        }


@dataclass(frozen=True)
class AgentRegistryProjection:
    """The whole fold: every principal the chains under one root establish."""

    agents: tuple[AgentPrincipal, ...] = ()
    errors: tuple[str, ...] = ()
    as_of: int = 0

    def agent(self, agent_id: str) -> AgentPrincipal | None:
        """Return the entry for ``agent_id``, or ``None`` when it is absent."""
        for entry in self.agents:
            if entry.agent_id == agent_id:
                return entry
        return None

    def to_body(self) -> dict[str, Any]:
        """Return the canonical dict :func:`render_registry` serialises."""
        return {
            "agents": [a.to_body() for a in self.agents],
            "as_of": self.as_of,
            "errors": list(self.errors),
            "version": REGISTRY_VERSION,
        }


@dataclass(frozen=True)
class RegistryVerification:
    """Outcome of recomputing a stored projection from the chain."""

    ok: bool
    reason: str
    invented: tuple[str, ...] = ()
    missing: tuple[str, ...] = ()


@dataclass
class _Builder:
    """Mutable accumulator for one principal while the fold runs."""

    agent_id: str
    events: list[ChainEventRef] = field(default_factory=list[ChainEventRef])
    ceiling: set[str] = field(default_factory=set[str])
    grant_ids: list[str] = field(default_factory=list[str])
    delegation_refs: list[str] = field(default_factory=list[str])


def _expand_ceiling(names: Iterable[str]) -> set[str]:
    """Project recorded ceiling tokens onto the ``PERM_*`` vocabulary.

    A grant records the coarse ``read``/``write``/``execute``/``full`` enum; a
    delegation scope records explicit ``PERM_*`` names. Expanding the former
    through the existing enum-to-permission map makes the two comparable
    without a second mapping living here. A token that is neither is carried
    through unchanged rather than dropped or guessed at.
    """
    expanded: set[str] = set()
    for name in names:
        if name in _COARSE_SCOPES:
            expanded |= set(scope_permissions(name))
        else:
            expanded.add(name)
    return expanded


def _run_ids(directory: Path) -> list[str]:
    """Return the ledger run ids under ``directory``, sorted for determinism."""
    if not directory.is_dir():
        return []
    return sorted(p.stem for p in directory.glob("*.jsonl"))


def _derive_spiffe_id(
    agent_id: str,
    *,
    trust_domain: str | None,
    install_public_key_pem: bytes | None,
) -> str | None:
    """Return the SPIFFE ID for ``agent_id``, or ``None`` when none is derivable.

    Three cases, in order:

    * the principal name already *is* a Bernstein SPIFFE ID (grant issuers run
      that way under the SPIFFE profile) -- it is carried through verbatim;
    * the caller supplied a trust domain and install key and the name is a
      valid SPIFFE path segment -- the id is derived the same way
      ``bernstein spiffe id`` derives it;
    * otherwise ``None``. A name like ``manager:test`` is not a SPIFFE path
      segment under this repository's grammar, and coining a substitute for it
      would be the same invention the projection refuses everywhere else.
    """
    try:
        return parse_spiffe_id(agent_id).uri
    except SpiffeIdError:
        pass
    if trust_domain is None or install_public_key_pem is None:
        return None
    try:
        validate_path_segment(agent_id)
        return derive_spiffe_id_from_key(
            trust_domain=trust_domain,
            install_public_key_pem=install_public_key_pem,
            agent_id=agent_id,
        )
    except SpiffeIdError:
        return None


def _note(builders: dict[str, _Builder], agent_id: str, event: ChainEventRef) -> _Builder | None:
    """Record ``event`` against ``agent_id``, creating the accumulator on demand."""
    if not agent_id:
        return None
    builder = builders.get(agent_id)
    if builder is None:
        builder = _Builder(agent_id=agent_id)
        builders[agent_id] = builder
    builder.events.append(event)
    return builder


def _fold_grants(builders: dict[str, _Builder], *, root: Path, key: bytes, now: int, errors: list[str]) -> None:
    """Fold every verified grant chain under ``root`` into ``builders``."""
    for run_id in _run_ids(Path(root) / "grants"):
        result = grants.verify_grant_chain(root=Path(root), run_id=run_id, key=key)
        if not result.valid:
            errors.append(f"grants/{run_id}: " + "; ".join(result.errors))
            continue
        for record in result.records:
            event_issuer = ChainEventRef(
                source=SOURCE_GRANT,
                run_id=run_id,
                index=record.record_index,
                kind=record.kind,
                role=_ROLE_ISSUER,
                anchor=record.hmac,
            )
            _note(builders, record.issuer, event_issuer)
            if record.audience:
                _note(
                    builders,
                    record.audience,
                    ChainEventRef(
                        source=SOURCE_GRANT,
                        run_id=run_id,
                        index=record.record_index,
                        kind=record.kind,
                        role=_ROLE_AUDIENCE,
                        anchor=record.hmac,
                    ),
                )
            if record.kind == grants.GRANT_ISSUED and record.audience:
                builders[record.audience].grant_ids.append(record.grant_id)

        issued = {r.grant_id: r for r in result.records if r.kind == grants.GRANT_ISSUED}
        for grant_id, state in result.lifecycles().items():
            issued_record = issued.get(grant_id)
            if issued_record is None or not state["issued"] or state["revoked"]:
                continue
            expiry = int(state["expiry"])
            if expiry and now >= expiry:
                continue
            if issued_record.audience is None:
                continue
            builder = builders.get(issued_record.audience)
            if builder is not None:
                builder.ceiling |= _expand_ceiling(issued_record.capability_ceiling)


def _fold_delegation(builders: dict[str, _Builder], *, root: Path, key: bytes, now: int, errors: list[str]) -> None:
    """Fold every tamper-verified delegation chain under ``root`` into ``builders``."""
    for run_id in _run_ids(Path(root) / "delegation"):
        result = delegation.verify_run_chain(root=Path(root), run_id=run_id, key=key)
        if not result.chain_ok:
            errors.append(f"delegation/{run_id}: " + "; ".join(result.errors))
            continue
        if not result.valid:
            # Tamper evidence held, so the hops are real events and their
            # parties are listed; the recomputed authority checks did not, so
            # nothing in this run is evidence of a ceiling in force.
            errors.append(f"delegation/{run_id}: authority checks did not pass; scopes not counted toward a ceiling")
        for receipt in result.receipts:
            for role, name in (
                (_ROLE_ISSUER, receipt.issuer),
                (_ROLE_SUBJECT, receipt.subject),
                (_ROLE_AUDIENCE, receipt.audience),
            ):
                _note(
                    builders,
                    name,
                    ChainEventRef(
                        source=SOURCE_DELEGATION,
                        run_id=run_id,
                        index=receipt.hop_index,
                        kind=receipt.act,
                        role=role,
                        anchor=receipt.hmac,
                    ),
                )
            if receipt.issuer:
                builders[receipt.issuer].delegation_refs.append(f"{run_id}#{receipt.hop_index}")
            if not result.valid or receipt.scope is None or not receipt.audience:
                continue
            scope = DelegationScope.from_body(receipt.scope)
            if scope.not_after is not None and now >= scope.not_after:
                continue
            builders[receipt.audience].ceiling |= _expand_ceiling(scope.permissions)


def project_agents(
    *,
    root: Path,
    key: bytes,
    now: int,
    trust_domain: str | None = None,
    install_public_key_pem: bytes | None = None,
) -> AgentRegistryProjection:
    """Fold the grant and delegation chains under ``root`` into a registry.

    Args:
        root: Audit root holding ``grants/`` and ``delegation/`` ledgers.
        key: The install audit HMAC key both chains were written with.
        now: The instant the ceiling is resolved at, as epoch seconds. Passed
            explicitly so two operators folding the same chain at the same
            instant render byte-identical output.
        trust_domain: Optional SPIFFE trust domain for id derivation.
        install_public_key_pem: Optional install public key (SPKI PEM bytes)
            for id derivation. Both must be supplied for a name to be derived.

    Returns:
        An :class:`AgentRegistryProjection` whose agents are sorted by id and
        whose per-agent lists are sorted, so the fold is a pure function of the
        chain content and ``now``.
    """
    builders: dict[str, _Builder] = {}
    errors: list[str] = []
    _fold_grants(builders, root=Path(root), key=key, now=now, errors=errors)
    _fold_delegation(builders, root=Path(root), key=key, now=now, errors=errors)

    agents = tuple(
        AgentPrincipal(
            agent_id=builder.agent_id,
            chain_events=tuple(sorted(builder.events, key=ChainEventRef.sort_key)),
            spiffe_id=_derive_spiffe_id(
                builder.agent_id,
                trust_domain=trust_domain,
                install_public_key_pem=install_public_key_pem,
            ),
            capability_ceiling=tuple(sorted(builder.ceiling)),
            grants=tuple(sorted(set(builder.grant_ids))),
            delegations=tuple(sorted(set(builder.delegation_refs))),
        )
        for builder in sorted(builders.values(), key=lambda b: b.agent_id)
    )
    return AgentRegistryProjection(agents=agents, errors=tuple(errors), as_of=int(now))


def render_registry(projection: AgentRegistryProjection) -> str:
    """Render ``projection`` as canonical JSON.

    Same construction as :func:`bernstein.core.identity.grants.render_report`:
    sorted keys, no incidental whitespace, so two verifiers folding the same
    chain at the same ``as_of`` produce byte-identical text that can be diffed
    across environments or handed to a third party.
    """
    return json.dumps(projection.to_body(), sort_keys=True, separators=(",", ":"))


def verify_registry(
    path: Path,
    *,
    root: Path,
    key: bytes,
    now: int,
    trust_domain: str | None = None,
    install_public_key_pem: bytes | None = None,
) -> RegistryVerification:
    """Recompute the projection from the chain and match it against ``path``.

    An agent the stored file lists but the chain does not establish is reported
    as *invented*; an agent the chain establishes but the file omits is
    reported as *missing*. Any remaining difference (a widened ceiling, a
    rewritten chain-event pointer) surfaces as a byte mismatch, because the
    canonical rendering leaves nowhere for it to hide.
    """
    try:
        parsed: object = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return RegistryVerification(ok=False, reason=f"cannot read stored projection: {exc}")
    if not isinstance(parsed, dict):
        return RegistryVerification(ok=False, reason="stored projection is not a JSON object")
    stored = cast("dict[str, Any]", parsed)

    recomputed = project_agents(
        root=Path(root),
        key=key,
        now=now,
        trust_domain=trust_domain,
        install_public_key_pem=install_public_key_pem,
    )
    expected_ids = {a.agent_id for a in recomputed.agents}
    raw_agents = stored.get("agents")
    if not isinstance(raw_agents, list):
        return RegistryVerification(ok=False, reason="stored projection has no agents list")
    stored_ids: set[str] = set()
    for entry in cast("list[Any]", raw_agents):
        if isinstance(entry, dict):
            stored_ids.add(str(cast("dict[str, Any]", entry).get("agent_id", "")))

    invented = tuple(sorted(stored_ids - expected_ids))
    missing = tuple(sorted(expected_ids - stored_ids))
    if invented or missing:
        parts: list[str] = []
        if invented:
            parts.append("not established by any chain event: " + ", ".join(invented))
        if missing:
            parts.append("established by the chain but absent: " + ", ".join(missing))
        return RegistryVerification(ok=False, reason="; ".join(parts), invented=invented, missing=missing)

    if json.dumps(stored, sort_keys=True, separators=(",", ":")) != render_registry(recomputed):
        return RegistryVerification(
            ok=False,
            reason="stored projection does not match the recomputation from the chain",
        )
    return RegistryVerification(ok=True, reason="every entry recomputes from the chain")
