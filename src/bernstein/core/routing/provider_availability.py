"""Per-role provider fallback chains with deterministic failover (issue #2355).

Upstream CLI backends and models disappear under operators with little
notice, and retry/routing today discovers a dead provider at first failure,
mid-run, usually unattended. This module makes provider availability an
explicit, replayable policy:

- **Per-role fallback chains**: an ordered list of ``(adapter, model)``
  pairs, each carrying a conformance level. A role declares a conformance
  floor; a chain element below the floor is rejected at validation time so a
  fallback is never silently less capable than the role requires.
- **Health probes**: a cheap per-provider liveness check executed before
  dispatch and cached for a configurable TTL. Probe outcomes are recorded,
  which makes every routing decision reproducible from the recorded probe
  set alone.
- **Deterministic failover**: :func:`decide_route` is a pure function of the
  declared chain and the probe outcomes. Two operators holding the same
  recorded probe set compute the byte-identical decision projection and the
  same ``sha256:`` decision hash. The decision is mirrored into the
  HMAC-chained audit log as a routing receipt (see
  :func:`bernstein.core.security.audit_chain.record_routing_failover_receipt`).
- **Failover drill**: :func:`run_failover_drill` exercises every declared
  chain position under a simulated outage of its predecessors, so operators
  find broken chains before an outage does (``bernstein doctor
  --failover-drill``).

The decision hash intentionally covers only the decision-determining
projection (chain, probe outcomes, chosen position, reason) -- never probe
timestamps or free-text details -- so a replay of the same recorded outcomes
always reproduces the same hash.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

#: Ordered conformance levels. A chain element must sit at or above the
#: role's declared floor. Mirrors the proficiency ordering used by
#: :class:`bernstein.core.routing.capability_router.CapabilityLevel`.
CONFORMANCE_ORDER: dict[str, int] = {"basic": 0, "advanced": 1, "expert": 2}

#: Default probe-result cache TTL in minutes.
DEFAULT_PROBE_TTL_MINUTES = 5

#: Routing decision reasons (recorded verbatim in the routing receipt).
REASON_PRIMARY_HEALTHY = "primary_healthy"
REASON_FAILOVER = "failover"
REASON_NO_HEALTHY_PROVIDER = "no_healthy_provider"

#: Probe kinds.
PROBE_KIND_BINARY_PATH = "binary_path"
PROBE_KIND_DISABLED = "disabled"
PROBE_KIND_DRILL_SIMULATED_OUTAGE = "drill_simulated_outage"


class AvailabilityPolicyError(ValueError):
    """Raised when a provider_availability section is malformed or a chain
    element falls below its role's conformance floor."""


# ---------------------------------------------------------------------------
# Policy model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ChainElement:
    """One ``(adapter, model)`` pair in a role's fallback chain.

    Attributes:
        adapter: CLI adapter name (e.g. ``claude``, ``codex``, ``gemini``).
        model: Model identifier dispatched through that adapter.
        conformance: Declared conformance level (``basic`` | ``advanced`` |
            ``expert``). Compared against the role's floor at validation.
    """

    adapter: str
    model: str
    conformance: str = "basic"

    def to_dict(self) -> dict[str, str]:
        return {"adapter": self.adapter, "model": self.model, "conformance": self.conformance}


@dataclass(frozen=True)
class RoleAvailabilityPolicy:
    """Declared fallback chain plus conformance floor for one role."""

    role: str
    conformance_floor: str
    chain: tuple[ChainElement, ...]


@dataclass(frozen=True)
class ProviderAvailabilityConfig:
    """Parsed ``provider_availability`` config section."""

    policies: dict[str, RoleAvailabilityPolicy] = field(default_factory=dict)
    probe_ttl_minutes: int = DEFAULT_PROBE_TTL_MINUTES
    probes_enabled: bool = True


def _require_level(value: object, *, context: str) -> str:
    level = str(value)
    if level not in CONFORMANCE_ORDER:
        raise AvailabilityPolicyError(
            f"{context}: unknown conformance level {level!r} (expected one of {sorted(CONFORMANCE_ORDER)})"
        )
    return level


def _parse_chain_element(raw: object, *, role: str, position: int) -> ChainElement:
    if not isinstance(raw, dict):
        raise AvailabilityPolicyError(
            f"provider_availability.roles.{role}.chain[{position}] must be a mapping, got {type(raw).__name__}"
        )
    entry = cast("dict[str, object]", raw)
    adapter = str(entry.get("adapter", "")).strip()
    model = str(entry.get("model", "")).strip()
    if not adapter or not model:
        raise AvailabilityPolicyError(
            f"provider_availability.roles.{role}.chain[{position}] needs non-empty 'adapter' and 'model'"
        )
    conformance = _require_level(
        entry.get("conformance", "basic"),
        context=f"provider_availability.roles.{role}.chain[{position}]",
    )
    return ChainElement(adapter=adapter, model=model, conformance=conformance)


def _parse_role_policy(role: str, raw: object) -> RoleAvailabilityPolicy:
    if not isinstance(raw, dict):
        raise AvailabilityPolicyError(f"provider_availability.roles.{role} must be a mapping, got {type(raw).__name__}")
    entry = cast("dict[str, object]", raw)
    floor = _require_level(
        entry.get("conformance_floor", "basic"),
        context=f"provider_availability.roles.{role}.conformance_floor",
    )
    chain_raw = entry.get("chain")
    if not isinstance(chain_raw, list) or not chain_raw:
        raise AvailabilityPolicyError(
            f"provider_availability.roles.{role}.chain must be a non-empty list of (adapter, model) mappings"
        )
    elements = tuple(
        _parse_chain_element(item, role=role, position=idx) for idx, item in enumerate(cast("list[object]", chain_raw))
    )
    for idx, element in enumerate(elements):
        if CONFORMANCE_ORDER[element.conformance] < CONFORMANCE_ORDER[floor]:
            raise AvailabilityPolicyError(
                f"provider_availability.roles.{role}: chain position {idx} "
                f"({element.adapter}/{element.model}) declares conformance "
                f"{element.conformance!r}, below the role's floor {floor!r}. "
                "A fallback must never be silently less capable than the role requires."
            )
    return RoleAvailabilityPolicy(role=role, conformance_floor=floor, chain=elements)


def parse_provider_availability(section: Mapping[str, object]) -> ProviderAvailabilityConfig:
    """Parse and validate a raw ``provider_availability`` config section.

    Args:
        section: Raw mapping from ``bernstein.yaml`` (``probe_ttl_minutes``,
            ``probes_enabled``, ``roles``).

    Returns:
        The validated :class:`ProviderAvailabilityConfig`.

    Raises:
        AvailabilityPolicyError: On any structural problem, unknown
            conformance level, empty chain, or a chain element below the
            role's conformance floor.
    """
    if not isinstance(section, dict):
        raise AvailabilityPolicyError(f"provider_availability must be a mapping, got {type(section).__name__}")
    data = cast("dict[str, object]", section)

    ttl_raw = data.get("probe_ttl_minutes", DEFAULT_PROBE_TTL_MINUTES)
    if not isinstance(ttl_raw, int) or isinstance(ttl_raw, bool) or ttl_raw < 1:
        raise AvailabilityPolicyError(
            f"provider_availability.probe_ttl_minutes must be a positive integer, got {ttl_raw!r}"
        )

    enabled_raw = data.get("probes_enabled", True)
    if not isinstance(enabled_raw, bool):
        raise AvailabilityPolicyError(
            f"provider_availability.probes_enabled must be a bool, got {type(enabled_raw).__name__}"
        )

    roles_raw = data.get("roles", {})
    if not isinstance(roles_raw, dict):
        raise AvailabilityPolicyError(f"provider_availability.roles must be a mapping, got {type(roles_raw).__name__}")

    policies = {
        str(role): _parse_role_policy(str(role), raw) for role, raw in cast("dict[str, object]", roles_raw).items()
    }
    return ProviderAvailabilityConfig(
        policies=policies,
        probe_ttl_minutes=int(ttl_raw),
        probes_enabled=enabled_raw,
    )


# ---------------------------------------------------------------------------
# Probes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProbeResult:
    """Outcome of one provider liveness probe.

    ``healthy``/``probe_kind``/``adapter`` are decision-determining and enter
    the decision hash; ``detail`` and ``checked_at`` are informational only.
    """

    adapter: str
    healthy: bool
    probe_kind: str
    detail: str = ""
    checked_at: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "adapter": self.adapter,
            "healthy": self.healthy,
            "probe_kind": self.probe_kind,
            "detail": self.detail,
            "checked_at": self.checked_at,
        }

    def hash_projection(self) -> dict[str, Any]:
        """The decision-determining slice of this probe result."""
        return {"adapter": self.adapter, "healthy": self.healthy, "probe_kind": self.probe_kind}


def binary_path_probe(element: ChainElement) -> ProbeResult:
    """Default cheap liveness probe: is the adapter binary on PATH?

    Deliberately network-free so offline runs and CI stay quiet; richer
    probes (endpoint reachability) can be injected by the caller.
    """
    found = shutil.which(element.adapter) is not None
    return ProbeResult(
        adapter=element.adapter,
        healthy=found,
        probe_kind=PROBE_KIND_BINARY_PATH,
        detail="found in PATH" if found else "not in PATH",
        checked_at=time.time(),
    )


def disabled_probe(element: ChainElement) -> ProbeResult:
    """Probe used when probing is disabled: every element presumed healthy.

    The presumption is recorded (``probe_kind="disabled"``) so a receipt
    reader can distinguish "probed healthy" from "not probed".
    """
    return ProbeResult(
        adapter=element.adapter,
        healthy=True,
        probe_kind=PROBE_KIND_DISABLED,
        detail="probing disabled",
        checked_at=time.time(),
    )


class ProbeCache:
    """Per-adapter probe-result cache with an explicit TTL.

    Probes are cheap but not free; dispatch bursts must not re-probe the
    same provider on every spawn. Entries expire after ``ttl_seconds``.
    """

    def __init__(self, ttl_seconds: float) -> None:
        self._ttl_seconds = float(ttl_seconds)
        self._entries: dict[str, tuple[float, ProbeResult]] = {}

    def get_or_probe(
        self,
        element: ChainElement,
        prober: Callable[[ChainElement], ProbeResult],
        *,
        now: float | None = None,
    ) -> ProbeResult:
        """Return a cached probe result for the element's adapter, probing on miss/expiry."""
        moment = time.time() if now is None else now
        cached = self._entries.get(element.adapter)
        if cached is not None:
            expires_at, result = cached
            if moment < expires_at:
                return result
        result = prober(element)
        self._entries[element.adapter] = (moment + self._ttl_seconds, result)
        return result


# ---------------------------------------------------------------------------
# Deterministic routing decision
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RoutingDecision:
    """Deterministic projection of one failover routing decision.

    The projection (and therefore :attr:`decision_hash`) is a pure function
    of the declared chain and the recorded probe outcomes: replaying the
    same recorded probe set reproduces the byte-identical projection.
    """

    role: str
    chain: tuple[ChainElement, ...]
    probes: tuple[ProbeResult, ...]
    chosen_index: int
    reason: str

    @property
    def chosen(self) -> ChainElement | None:
        """The selected chain element, or None when no element was healthy."""
        if 0 <= self.chosen_index < len(self.chain):
            return self.chain[self.chosen_index]
        return None

    def projection(self) -> dict[str, Any]:
        """Canonical decision-determining projection (hash input)."""
        return {
            "role": self.role,
            "chain": [element.to_dict() for element in self.chain],
            "probes": [probe.hash_projection() for probe in self.probes],
            "chosen_index": self.chosen_index,
            "reason": self.reason,
        }

    @property
    def decision_hash(self) -> str:
        """``sha256:`` hash of the canonical JSON projection."""
        canonical = json.dumps(self.projection(), sort_keys=True, separators=(",", ":"))
        return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def decide_route(policy: RoleAvailabilityPolicy, probes: Sequence[ProbeResult]) -> RoutingDecision:
    """Pick the first healthy chain element -- a pure, replayable function.

    Args:
        policy: The role's declared fallback chain.
        probes: Probe results aligned position-for-position with the chain.

    Returns:
        The :class:`RoutingDecision`. ``chosen_index`` is ``-1`` (and the
        reason :data:`REASON_NO_HEALTHY_PROVIDER`) when no element is healthy.

    Raises:
        AvailabilityPolicyError: When the probe sequence does not align with
            the chain (a misaligned recording cannot be replayed honestly).
    """
    if len(probes) != len(policy.chain):
        raise AvailabilityPolicyError(
            f"probe results for role {policy.role!r} must align with the chain: "
            f"{len(policy.chain)} elements, {len(probes)} probes"
        )
    chosen_index = -1
    for idx, probe in enumerate(probes):
        if probe.healthy:
            chosen_index = idx
            break
    if chosen_index == -1:
        reason = REASON_NO_HEALTHY_PROVIDER
    elif chosen_index == 0:
        reason = REASON_PRIMARY_HEALTHY
    else:
        reason = REASON_FAILOVER
    return RoutingDecision(
        role=policy.role,
        chain=policy.chain,
        probes=tuple(probes),
        chosen_index=chosen_index,
        reason=reason,
    )


def resolve_route(
    policy: RoleAvailabilityPolicy,
    *,
    cache: ProbeCache,
    prober: Callable[[ChainElement], ProbeResult],
    probes_enabled: bool = True,
) -> RoutingDecision:
    """Probe every chain element (via the cache) and decide the route.

    When ``probes_enabled`` is False (offline runs), every element is
    presumed healthy and the presumption is recorded per element.
    """
    effective_prober = prober if probes_enabled else disabled_probe
    if probes_enabled:
        probes = tuple(cache.get_or_probe(element, effective_prober) for element in policy.chain)
    else:
        probes = tuple(effective_prober(element) for element in policy.chain)
    return decide_route(policy, probes)


# ---------------------------------------------------------------------------
# Failover drill
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DrillElementResult:
    """Drill outcome for one chain position of one role.

    ``decision_hash`` is the deterministic routing decision computed with
    every predecessor position simulated as an outage -- the exact decision
    dispatch would make if the chain degraded to this position.
    """

    role: str
    position: int
    adapter: str
    model: str
    healthy: bool
    detail: str
    decision_hash: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "position": self.position,
            "adapter": self.adapter,
            "model": self.model,
            "healthy": self.healthy,
            "detail": self.detail,
            "decision_hash": self.decision_hash,
        }


@dataclass(frozen=True)
class DrillReport:
    """Aggregated failover-drill outcome across every declared role chain."""

    elements: tuple[DrillElementResult, ...]

    @property
    def broken_roles(self) -> tuple[str, ...]:
        """Roles with at least one unhealthy chain element, sorted."""
        return tuple(sorted({row.role for row in self.elements if not row.healthy}))

    @property
    def ok(self) -> bool:
        """True when every declared chain element is healthy."""
        return not self.broken_roles

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "broken_roles": list(self.broken_roles),
            "elements": [row.to_dict() for row in self.elements],
        }


def run_failover_drill(
    config: ProviderAvailabilityConfig,
    prober: Callable[[ChainElement], ProbeResult] | None = None,
) -> DrillReport:
    """Exercise every declared fallback path and report broken chain elements.

    For each role, every chain element is probed live (no cache -- a drill
    wants current truth). Each position is then evaluated as the dispatch
    target under a simulated outage of all its predecessors, producing the
    deterministic decision hash an operator can later compare against a real
    outage's routing receipt.

    Args:
        config: The parsed availability config.
        prober: Probe function; defaults to :func:`binary_path_probe`.

    Returns:
        The :class:`DrillReport`; ``report.ok`` is False when any declared
        chain element is unhealthy.
    """
    effective_prober = prober or binary_path_probe
    rows: list[DrillElementResult] = []
    for role in sorted(config.policies):
        policy = config.policies[role]
        live_probes = [effective_prober(element) for element in policy.chain]
        for position, element in enumerate(policy.chain):
            simulated = [
                ProbeResult(
                    adapter=policy.chain[idx].adapter,
                    healthy=False,
                    probe_kind=PROBE_KIND_DRILL_SIMULATED_OUTAGE,
                    detail="simulated outage (drill)",
                    checked_at=live_probes[idx].checked_at,
                )
                if idx < position
                else live_probes[idx]
                for idx in range(len(policy.chain))
            ]
            decision = decide_route(policy, simulated)
            rows.append(
                DrillElementResult(
                    role=role,
                    position=position,
                    adapter=element.adapter,
                    model=element.model,
                    healthy=live_probes[position].healthy,
                    detail=live_probes[position].detail,
                    decision_hash=decision.decision_hash,
                )
            )
    return DrillReport(elements=tuple(rows))


__all__ = [
    "CONFORMANCE_ORDER",
    "DEFAULT_PROBE_TTL_MINUTES",
    "PROBE_KIND_BINARY_PATH",
    "PROBE_KIND_DISABLED",
    "PROBE_KIND_DRILL_SIMULATED_OUTAGE",
    "REASON_FAILOVER",
    "REASON_NO_HEALTHY_PROVIDER",
    "REASON_PRIMARY_HEALTHY",
    "AvailabilityPolicyError",
    "ChainElement",
    "DrillElementResult",
    "DrillReport",
    "ProbeCache",
    "ProbeResult",
    "ProviderAvailabilityConfig",
    "RoleAvailabilityPolicy",
    "RoutingDecision",
    "binary_path_probe",
    "decide_route",
    "disabled_probe",
    "parse_provider_availability",
    "resolve_route",
    "run_failover_drill",
]
