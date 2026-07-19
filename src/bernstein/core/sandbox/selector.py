"""Sandbox backend selection policy.

Picks a sandbox backend deterministically given a task spec, a cost
budget, the operator's policy, and the credentials currently visible
to the orchestrator. The selector is a pure function over its inputs:
no I/O, no registry side-effects. Callers compose it with
:func:`bernstein.core.sandbox.registry.get_backend` to materialise the
chosen backend instance.

Design goals:

- **Deterministic**: same inputs always produce the same backend pick
  so plan replays and dry-runs agree with live runs.
- **Override-first**: an explicit ``--sandbox <name>`` CLI flag (or the
  equivalent policy field) wins over every heuristic, even when the
  named backend lacks credentials. The selector reports the conflict
  via :class:`SandboxSelectionError` so the operator sees the failure
  loudly instead of silently falling back to a different runtime.
- **Cost-aware**: when no override is set, the selector prefers cheaper
  backends first (local Docker/worktree) and only escalates to paid
  cloud backends (e2b, Modal, Daytona) when the manifest demands a
  capability the cheaper backends cannot serve, or when the operator
  explicitly opted into paid execution via the policy.
- **Capability-gated**: backends that cannot satisfy the manifest's
  required capability set are filtered out before precedence is
  applied. Operators reading the selector's logs can tell at a glance
  why a given backend was skipped.

The deterministic precedence (lowest-cost-first) is::

    worktree -> docker -> e2b -> modal -> daytona -> blaxel
              -> runloop -> vercel

Backends not present in :data:`DEFAULT_PRECEDENCE` are appended in
sorted-name order so plug-in backends still get a stable position.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from bernstein.core.sandbox.backend import SandboxCapability

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from bernstein.core.sandbox.backend import SandboxBackend


#: Lowest-cost-first ordering used when no override is set. Adding a
#: new backend here is the canonical way to teach the selector about
#: it; backends not listed are treated as "unknown cost" and ranked
#: alphabetically after the named ones.
DEFAULT_PRECEDENCE: tuple[str, ...] = (
    "worktree",
    "docker",
    "e2b",
    "modal",
    "daytona",
    "blaxel",
    "runloop",
    "vercel",
    # microvm is opt-in: it is not a FREE_BACKEND, so the heuristic path
    # never auto-selects it over worktree/docker. An explicit
    # ``sandbox.backend: microvm`` override reaches it, and an unsupported
    # host then fails loudly at create() (MicroVMUnavailableError) rather
    # than silently degrading to a bare worktree.
    "microvm",
)


class SandboxSelectionError(RuntimeError):
    """Raised when no backend can satisfy the policy + manifest pair.

    Carries a structured ``reason`` for callers that want to surface
    the failure to the operator without re-parsing the message.
    """

    def __init__(self, reason: str, *, attempted: Sequence[str] = ()) -> None:
        super().__init__(reason)
        self.reason = reason
        self.attempted: tuple[str, ...] = tuple(attempted)


@dataclass(frozen=True)
class SandboxPolicy:
    """Operator-supplied selection policy.

    All fields default to "unrestricted" so the simplest invocation -
    :class:`SandboxPolicy()` - picks the cheapest backend that can
    satisfy the manifest. Operators tighten the policy by setting one
    or more fields; the selector treats every field as an AND-condition
    against the candidate backend list.

    Attributes:
        override: Explicit backend name forced by the caller (e.g.
            ``--sandbox docker``). When set, the selector either picks
            this backend (after capability + credential checks) or
            raises :class:`SandboxSelectionError`. Defaults to ``None``
            for "no override".
        allow_paid: When ``False`` (default), only backends listed in
            :data:`FREE_BACKENDS` are considered. Setting ``True``
            unlocks paid cloud backends (``e2b``, ``modal`` ...). The
            cost autopilot will eventually flip this based on remaining
            budget.
        required_capabilities: Capabilities the chosen backend MUST
            advertise. Backends missing any of these are dropped from
            consideration.
        required_credentials: Names of environment variables that must
            be present (and non-empty) in :class:`SandboxEnvironment`
            for the backend to be considered usable. Used by the
            selector to skip cloud backends when their API key is
            absent.
        precedence: Optional override of :data:`DEFAULT_PRECEDENCE`.
            Useful for tests and for operators who prefer e.g. e2b
            over docker. The list need not be exhaustive - backends
            not mentioned are appended alphabetically.
    """

    override: str | None = None
    allow_paid: bool = False
    required_capabilities: frozenset[SandboxCapability] = field(
        default_factory=lambda: frozenset({SandboxCapability.FILE_RW, SandboxCapability.EXEC})
    )
    required_credentials: frozenset[str] = field(default_factory=frozenset)
    precedence: tuple[str, ...] | None = None


@dataclass(frozen=True)
class SandboxEnvironment:
    """Snapshot of orchestrator-side state the selector reads.

    Kept as an explicit value object instead of probing ``os.environ``
    inside the selector so unit tests can pass synthetic data without
    monkey-patching the process environment, and so the cost-autopilot
    ticket can later inject a derived environment that already accounts
    for budget consumption.

    Attributes:
        available_credentials: Set of environment-variable names the
            orchestrator has discovered. The selector uses this both
            to filter cloud backends with absent API keys and to satisfy
            :attr:`SandboxPolicy.required_credentials`.
        budget_remaining_usd: Optional remaining cost budget in USD.
            ``None`` means "no budget enforcement"; ``0`` or negative
            forces the selector to fall back to free backends only.
    """

    available_credentials: frozenset[str] = field(default_factory=frozenset)
    budget_remaining_usd: float | None = None


#: Backends that incur no per-second cost and require no API key.
FREE_BACKENDS: frozenset[str] = frozenset({"worktree", "docker"})

#: Mapping from backend name to the credential env-var the backend
#: needs to function. Backends not in this map are assumed credential-
#: free (true for ``worktree``/``docker``).
BACKEND_CREDENTIAL_ENVS: dict[str, frozenset[str]] = {
    "e2b": frozenset({"E2B_API_KEY"}),
    "modal": frozenset({"MODAL_TOKEN_ID", "MODAL_TOKEN_SECRET"}),
    "daytona": frozenset({"DAYTONA_API_KEY"}),
    "blaxel": frozenset({"BLAXEL_API_KEY"}),
    "runloop": frozenset({"RUNLOOP_API_KEY"}),
    "vercel": frozenset({"VERCEL_TOKEN"}),
}


def select_sandbox(
    backends: Iterable[SandboxBackend],
    *,
    policy: SandboxPolicy | None = None,
    environment: SandboxEnvironment | None = None,
) -> SandboxBackend:
    """Pick the sandbox backend that best satisfies *policy*.

    The selector is pure: it never instantiates new backends and never
    touches the registry. Callers materialise *backends* via
    :func:`bernstein.core.sandbox.registry.list_backends` (or a filtered
    subset) and pass the result.

    Args:
        backends: Iterable of registered backends to choose from.
            Order is irrelevant; precedence is applied internally.
        policy: Selection policy. ``None`` uses :class:`SandboxPolicy`
            defaults (cheapest backend, paid disallowed, FILE_RW + EXEC
            required).
        environment: Snapshot of orchestrator-side state. ``None`` means
            no credentials and no budget tracking - equivalent to a
            fresh process with an empty environment.

    Returns:
        The chosen :class:`SandboxBackend`. Always one of the supplied
        backends - the selector never instantiates new ones.

    Raises:
        SandboxSelectionError: When no backend in *backends* satisfies
            the policy. The error's ``attempted`` attribute lists the
            backends considered, which is helpful for surfacing the
            reason to operators.
    """
    effective_policy = policy or SandboxPolicy()
    effective_env = environment or SandboxEnvironment()
    candidates = list(backends)
    if not candidates:
        raise SandboxSelectionError("No sandbox backends registered", attempted=())

    if effective_policy.override is not None:
        return _resolve_override(candidates, effective_policy, effective_env)

    eligible = _filter_eligible(candidates, effective_policy, effective_env)
    if not eligible:
        raise SandboxSelectionError(
            "No sandbox backend satisfies the policy",
            attempted=tuple(b.name for b in candidates),
        )

    ordered = _apply_precedence(eligible, effective_policy.precedence)
    return ordered[0]


def _resolve_override(
    backends: Sequence[SandboxBackend],
    policy: SandboxPolicy,
    environment: SandboxEnvironment,
) -> SandboxBackend:
    """Honour ``policy.override`` or raise a precise error.

    Override-first means the operator's intent wins, but we still
    refuse to pretend the backend is usable when its capability or
    credential preconditions are not met - silent fallbacks have
    bitten us before in surfaces where ``--sandbox`` was treated as a
    hint rather than a contract.
    """
    name = policy.override
    by_name = {b.name: b for b in backends}
    candidate = by_name.get(name) if name is not None else None
    if candidate is None:
        raise SandboxSelectionError(
            f"Override sandbox {name!r} is not registered",
            attempted=tuple(by_name.keys()),
        )
    missing_caps = policy.required_capabilities - candidate.capabilities
    if missing_caps:
        missing = ", ".join(sorted(c.value for c in missing_caps))
        raise SandboxSelectionError(
            f"Override {name!r} is missing capabilities: {missing}",
            attempted=(candidate.name,),
        )
    missing_creds = _missing_credentials(candidate, policy, environment)
    if missing_creds:
        creds = ", ".join(sorted(missing_creds))
        raise SandboxSelectionError(
            f"Override {name!r} missing credentials: {creds}",
            attempted=(candidate.name,),
        )
    return candidate


def _filter_eligible(
    backends: Sequence[SandboxBackend],
    policy: SandboxPolicy,
    environment: SandboxEnvironment,
) -> list[SandboxBackend]:
    """Drop backends that cannot satisfy *policy* given *environment*."""
    eligible: list[SandboxBackend] = []
    out_of_budget = environment.budget_remaining_usd is not None and environment.budget_remaining_usd <= 0
    for backend in backends:
        if not policy.allow_paid and backend.name not in FREE_BACKENDS:
            continue
        if out_of_budget and backend.name not in FREE_BACKENDS:
            continue
        if policy.required_capabilities - backend.capabilities:
            continue
        if _missing_credentials(backend, policy, environment):
            continue
        eligible.append(backend)
    return eligible


def _missing_credentials(
    backend: SandboxBackend,
    policy: SandboxPolicy,
    environment: SandboxEnvironment,
) -> frozenset[str]:
    """Return required env-var names that *environment* does not supply."""
    needed = set(policy.required_credentials)
    needed |= BACKEND_CREDENTIAL_ENVS.get(backend.name, frozenset())
    return frozenset(needed - environment.available_credentials)


def _apply_precedence(
    backends: Sequence[SandboxBackend],
    override: tuple[str, ...] | None,
) -> list[SandboxBackend]:
    """Return *backends* sorted by precedence (lowest cost first)."""
    order = override if override is not None else DEFAULT_PRECEDENCE
    rank = {name: index for index, name in enumerate(order)}
    fallback_rank = len(order)

    def _key(backend: SandboxBackend) -> tuple[int, str]:
        return (rank.get(backend.name, fallback_rank), backend.name)

    return sorted(backends, key=_key)


__all__ = [
    "BACKEND_CREDENTIAL_ENVS",
    "DEFAULT_PRECEDENCE",
    "FREE_BACKENDS",
    "SandboxEnvironment",
    "SandboxPolicy",
    "SandboxSelectionError",
    "select_sandbox",
]
