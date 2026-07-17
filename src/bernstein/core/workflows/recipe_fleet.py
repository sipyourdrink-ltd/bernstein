"""Declarative fleet manifest: plan and apply for registered recipes (#2546).

A fleet manifest declares the desired set of registered recipes.
``recipes plan`` emits a canonical, byte-reproducible diff document with a
``plan_hash`` a reviewer can approve; ``recipes apply --plan <hash>``
refuses to run against any other registry state and writes an apply receipt
bound to that exact plan hash. Re-running plan against mutated state detects
drift as a different ``plan_hash``. "The fleet change that ran is provably
the change that was approved" becomes a hash equality.

The plan is a pure projection of ``(current live state, desired target
state)``: both are content hashes, so two operators over identical state
emit byte-identical plan documents.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING

from bernstein.core.orchestration.collision import CollisionPolicy
from bernstein.core.workflows.recipe_registry import (
    RecipePins,
    compute_recipe_registration,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from bernstein.core.workflows.recipe_registry import RecipeRegistry
    from bernstein.core.workflows.recipe_spec import RecipeSpec

__all__ = [
    "FLEET_PLAN_REV",
    "FleetDriftError",
    "FleetPlan",
    "ManifestEntry",
    "apply_fleet",
    "plan_fleet",
]

#: Schema rev baked into a fleet plan document; bumping it changes plan_hash.
FLEET_PLAN_REV = "1"


class FleetDriftError(RuntimeError):
    """Raised when apply runs against registry state that drifted from the plan.

    The message names the divergent recipe (name + expected vs actual live
    hash) so an operator can see exactly which definition moved.
    """


@dataclass(frozen=True)
class ManifestEntry:
    """One desired registered recipe in a fleet manifest."""

    spec: RecipeSpec
    pins: RecipePins | None = None
    collision_policy: CollisionPolicy | str = CollisionPolicy.CANCEL_NEW
    concurrency_cap: int = 1
    sandbox_pool: str = ""

    def target_hash(self) -> str:
        _bytes, digest, *_rest = compute_recipe_registration(
            self.spec,
            pins=self.pins,
            collision_policy=self.collision_policy,
            concurrency_cap=self.concurrency_cap,
            sandbox_pool=self.sandbox_pool,
        )
        return digest


@dataclass(frozen=True)
class FleetPlan:
    """A byte-reproducible fleet plan and its hash.

    Attributes:
        base_state: Sorted ``name -> live_hash`` the plan was computed
            against (``""`` when a name is unregistered).
        target_state: Sorted ``name -> desired_hash``.
        to_register: Names present in target but not live.
        to_supersede: Names whose live hash differs from the target.
        unchanged: Names already at the target hash.
        plan_bytes: The exact canonical bytes ``plan_hash`` is taken over.
        plan_hash: SHA-256 of ``plan_bytes``.
    """

    base_state: dict[str, str]
    target_state: dict[str, str]
    to_register: tuple[str, ...]
    to_supersede: tuple[str, ...]
    unchanged: tuple[str, ...]
    plan_bytes: bytes
    plan_hash: str

    def to_document(self) -> dict[str, object]:
        """Return the JSON-safe plan document for ``recipes plan`` output."""
        return json.loads(self.plan_bytes.decode())


def plan_fleet(registry: RecipeRegistry, manifest: Sequence[ManifestEntry]) -> FleetPlan:
    """Compute a byte-reproducible plan from *manifest* against *registry*.

    Running twice against the same registry state yields byte-identical
    ``plan_bytes`` and equal ``plan_hash`` (AC7).
    """
    target_state: dict[str, str] = {}
    for entry in manifest:
        target_state[entry.spec.name] = entry.target_hash()

    names = sorted({*target_state})
    base_state: dict[str, str] = {name: (registry.live_hash(name) or "") for name in names}

    to_register: list[str] = []
    to_supersede: list[str] = []
    unchanged: list[str] = []
    for name in names:
        live = base_state[name]
        target = target_state[name]
        if not live:
            to_register.append(name)
        elif live != target:
            to_supersede.append(name)
        else:
            unchanged.append(name)

    plan_obj = {
        "rev": FLEET_PLAN_REV,
        "base_state": {k: base_state[k] for k in sorted(base_state)},
        "target_state": {k: target_state[k] for k in sorted(target_state)},
    }
    plan_bytes = json.dumps(plan_obj, sort_keys=True, separators=(",", ":")).encode()
    plan_hash = hashlib.sha256(plan_bytes).hexdigest()
    return FleetPlan(
        base_state=base_state,
        target_state=target_state,
        to_register=tuple(to_register),
        to_supersede=tuple(to_supersede),
        unchanged=tuple(unchanged),
        plan_bytes=plan_bytes,
        plan_hash=plan_hash,
    )


def apply_fleet(
    registry: RecipeRegistry,
    manifest: Sequence[ManifestEntry],
    *,
    plan_hash: str,
    actor: str = "operator",
) -> tuple[str, ...]:
    """Apply *manifest* iff the registry still matches the plan's base state.

    Recomputes the plan against the live registry. When the recomputed
    ``plan_hash`` differs from the approved *plan_hash* the registry drifted
    since the plan was reviewed; :class:`FleetDriftError` is raised naming
    the divergent recipe. On a match every register / supersede in the plan
    is applied and a ``recipe.fleet_apply`` receipt bound to *plan_hash* is
    written.

    Returns the tuple of applied names.

    Raises:
        FleetDriftError: When the live registry state differs from the plan.
    """
    current = plan_fleet(registry, manifest)
    if current.plan_hash != plan_hash:
        divergent = _first_divergence(current, plan_hash, registry, manifest)
        raise FleetDriftError(
            f"registry state drifted from approved plan {plan_hash[:16]}: {divergent}; "
            f"current plan hash is {current.plan_hash[:16]}",
        )

    applied: list[str] = []
    entries = {e.spec.name: e for e in manifest}
    for name in (*current.to_register, *current.to_supersede):
        entry = entries[name]
        registry.register(
            spec=entry.spec,
            pins=entry.pins,
            collision_policy=entry.collision_policy,
            concurrency_cap=entry.concurrency_cap,
            sandbox_pool=entry.sandbox_pool,
            actor=actor,
        )
        applied.append(name)

    from bernstein.core.security.audit_chain import record_recipe_fleet_apply

    record_recipe_fleet_apply(
        chain=registry._get_chain(),
        plan_hash=plan_hash,
        applied=tuple(applied),
        actor=actor,
    )
    return tuple(applied)


def _first_divergence(
    current: FleetPlan,
    approved_plan_hash: str,
    registry: RecipeRegistry,
    manifest: Sequence[ManifestEntry],
) -> str:
    """Describe the first recipe whose live hash no longer matches the plan."""
    for name in sorted(current.base_state):
        live = registry.live_hash(name) or ""
        target = current.target_state.get(name, "")
        if live and live != target:
            return f"recipe {name!r} is live at {live[:16]} not the planned {target[:16] or '(unregistered)'}"
    return f"plan hash mismatch (approved {approved_plan_hash[:16]})"
