"""Content-addressed registry for registered recipes (#2546).

A *registered recipe* is a content-addressed run definition whose entire
lifecycle lives on the HMAC audit chain. The recipe's identity is the
SHA-256 of its canonical body - not a filename, not a database row - so two
fires under different hashes are definitionally different runs and drift is
hash divergence, answerable offline. There is no mutable registry row: the
live ``name -> hash`` mapping and the paused state are a pure projection of
the register / supersede / rollback / pause / resume receipts.

This generalises the ``sched_`` content-hash discipline in
:mod:`bernstein.core.planning.schedule_store` from a schedule to a whole
run definition, and extends the receipt coverage that already anchors the
fire instant (journal + spine + chain) to the definition lifecycle:

- **register** seals the canonical bytes into the lineage spine and writes a
  ``recipe.register`` receipt; re-registering a changed body under the same
  name writes an operator-signed ``recipe.supersede`` (old_hash, new_hash).
- **rollback** re-points the name at a prior hash via a new receipt; nothing
  is deleted.
- **pause / resume** are definition-level state records; a paused recipe
  fires nothing yet keeps its identity and history.
- **history --verify** walks the receipts against the HMAC chain offline and
  fails on any broken or reordered link.

Killer shape: the recipe *is* its content-addressed, spine-sealed
definition and a fire is a deterministic projection of it. Strip the
content-addressing and the lineage spine and it degrades to a stored job
with a log.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from bernstein.core.orchestration.collision import CollisionPolicy
from bernstein.core.orchestration.recurrence import canonicalise_recurrence
from bernstein.core.orchestration.schedule_kinds import (
    DstPolicy,
    ScheduleKind,
    ScheduleKindError,
    canonical_timezone,
)

if TYPE_CHECKING:
    from pathlib import Path

    from bernstein.core.security.audit_chain import AuditChainStore
    from bernstein.core.workflows.recipe_spec import RecipeSpec

__all__ = [
    "CANONICAL_RECIPE_REV",
    "RecipeFireResult",
    "RecipePins",
    "RecipeRegistry",
    "RecipeRegistryError",
    "RecipeSchedule",
    "RegisteredRecipe",
    "canonical_recipe_bytes",
    "compute_recipe_registration",
    "current_git_commit",
    "recipe_content_hash",
    "recipe_id",
    "recipe_run_id",
]

#: Schema rev baked into the canonical recipe body. Bumping it changes every
#: recipe_hash and is the single lever for evolving the canonical encoding.
CANONICAL_RECIPE_REV = "1"

_LINEAGE_SUBDIR = "lineage"
_AUDIT_SUBDIR = "audit"


class RecipeRegistryError(ValueError):
    """Raised when a recipe registration or lookup is invalid."""


@dataclass(frozen=True)
class RecipePins:
    """Pinned inputs folded into the canonical recipe body.

    Every pin is content that, if it changes, makes the run a different run:
    the git commit of the recipe source, the adapter, the model, and the
    prompt-pack hash. Two operators at the same commit with the same pins
    derive the byte-identical canonical body; changing any pin changes the
    ``recipe_hash``.
    """

    git_commit: str = ""
    adapter: str = ""
    model: str = ""
    prompt_pack_sha256: str = ""

    def canonical_dict(self) -> dict[str, str]:
        return {
            "git_commit": self.git_commit,
            "adapter": self.adapter,
            "model": self.model,
            "prompt_pack_sha256": self.prompt_pack_sha256,
        }


@dataclass(frozen=True)
class RecipeSchedule:
    """A parsed, canonicalised schedule declared on a recipe.

    ``kind`` selects how ``recurrence`` / ``interval_seconds`` / ``anchor``
    are interpreted; ``timezone`` and ``dst_policy`` govern local-time and
    DST resolution. All fields are canonical so two operators who wrote the
    same schedule in different token order fold to the same bytes.
    """

    kind: ScheduleKind
    recurrence: str = ""
    interval_seconds: int = 0
    anchor: int = 0
    timezone: str = ""
    dst_policy: str = str(DstPolicy.PRE_TRANSITION)

    def canonical_dict(self) -> dict[str, Any]:
        return {
            "kind": str(self.kind),
            "recurrence": self.recurrence,
            "interval_seconds": self.interval_seconds,
            "anchor": self.anchor,
            "timezone": self.timezone,
            "dst_policy": self.dst_policy,
        }

    @classmethod
    def from_mapping(cls, raw: dict[str, Any]) -> RecipeSchedule:
        """Parse and canonicalise one schedule mapping from a manifest.

        Raises:
            ScheduleKindError: On an unknown kind, a bad recurrence, an
                unknown timezone, or a non-positive interval.
        """
        kind_raw = str(raw.get("kind", ScheduleKind.CRON))
        try:
            kind = ScheduleKind(kind_raw)
        except ValueError as exc:
            raise ScheduleKindError(f"unknown schedule kind {kind_raw!r}") from exc

        timezone = canonical_timezone(str(raw.get("timezone", "")))
        dst_policy = str(DstPolicy(str(raw.get("dst_policy", DstPolicy.PRE_TRANSITION))))

        recurrence = ""
        interval_seconds = 0
        anchor = int(raw.get("anchor", 0))
        if kind is ScheduleKind.INTERVAL_ANCHOR:
            interval_seconds = int(raw.get("interval_seconds", 0))
            if interval_seconds <= 0:
                raise ScheduleKindError("interval_anchor schedule requires positive interval_seconds")
        else:
            rule = str(raw.get("recurrence", "") or raw.get("cron", "") or raw.get("rrule", ""))
            recurrence = canonicalise_recurrence(rule) if rule else ""
            if not recurrence:
                raise ScheduleKindError(f"{kind} schedule requires a recurrence / cron / rrule expression")
        return cls(
            kind=kind,
            recurrence=recurrence,
            interval_seconds=interval_seconds,
            anchor=anchor,
            timezone=timezone,
            dst_policy=dst_policy,
        )


@dataclass(frozen=True)
class RegisteredRecipe:
    """A recipe as registered: its content hash and lifecycle state.

    Attributes:
        name: Operator-facing name the hash is registered under.
        recipe_hash: SHA-256 of the canonical body; the recipe identity.
        canonical_bytes: The exact bytes the hash is taken over.
        spine_anchor: Lineage-spine entry hash the bytes were sealed into.
        schedules: Parsed schedules declared on the recipe.
        collision_policy: Concurrency-collision strategy.
        concurrency_cap: Max concurrent fires allowed.
        pins: Pinned inputs folded into the hash.
        superseded_hash: The prior live hash this registration replaced, or
            ``""`` for a first registration.
        registered_at: Wall-clock epoch of the registration (bookkeeping;
            not part of the hash).
    """

    name: str
    recipe_hash: str
    canonical_bytes: bytes
    spine_anchor: str
    schedules: tuple[RecipeSchedule, ...]
    collision_policy: CollisionPolicy
    concurrency_cap: int
    pins: RecipePins
    superseded_hash: str = ""
    registered_at: float = 0.0

    @property
    def recipe_id(self) -> str:
        """Grep-friendly short identity, ``recipe_<12hex>``."""
        return recipe_id(self.recipe_hash)


@dataclass(frozen=True)
class RecipeFireResult:
    """Outcome of firing a registered recipe by name.

    ``dispatched`` is False when the recipe is paused (it fires nothing).
    ``projection_hash`` is the deterministic fire projection hash plus its
    chain anchor - the fire is a hash rather than an opaque job id.
    """

    name: str
    recipe_hash: str
    dispatched: bool
    fire_time: int
    projection_hash: str = ""
    chain_anchor: str = ""
    reason: str = ""


def recipe_content_hash(canonical_bytes: bytes) -> str:
    """Return the SHA-256 hex of the canonical recipe body (the identity)."""
    return hashlib.sha256(canonical_bytes).hexdigest()


def recipe_id(recipe_hash: str) -> str:
    """Return the grep-friendly short form of a recipe hash."""
    return f"recipe_{recipe_hash[:12]}"


def recipe_run_id(recipe_hash: str) -> str:
    """Return a deterministic, separator-free run id for a recipe hash.

    Used to key the lineage-spine run the canonical bytes seal into, so two
    operators land on the same spine run for the same definition.
    """
    return "recipe-def-" + recipe_hash[:16]


def current_git_commit(workdir: Path | None = None) -> str:
    """Return the current git HEAD commit, or ``""`` when unavailable.

    Pins the recipe source revision into the canonical body. Failures
    (not a repo, git missing) degrade to ``""`` rather than raising, so
    registration outside a checkout still works with an empty pin.
    """
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(workdir) if workdir is not None else None,
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def canonical_recipe_bytes(
    *,
    spec: RecipeSpec,
    pins: RecipePins,
    schedules: tuple[RecipeSchedule, ...],
    collision_policy: CollisionPolicy,
    concurrency_cap: int,
    sandbox_pool: str,
    triggers: tuple[dict[str, Any], ...],
) -> bytes:
    """Encode the canonical recipe body to byte-reproducible JSON.

    The body pins the git commit plus adapter / model / prompt-pack hashes
    and folds the param schema, workflow nodes, schedules, triggers, sandbox
    pool, and collision policy. Two operators with the same manifest at the
    same commit derive byte-identical bytes; changing any pinned input
    changes the bytes and therefore the hash (AC1).
    """
    param_schema = [
        {
            "name": p.name,
            "type": p.type,
            "default": p.default,
            "required": p.required,
            "choices": sorted(p.choices) if p.choices else None,
        }
        for p in sorted(spec.params, key=lambda p: p.name)
    ]
    body: dict[str, Any] = {
        "rev": CANONICAL_RECIPE_REV,
        "name": spec.name,
        "description": spec.description,
        "version": spec.version,
        "params": param_schema,
        "nodes": spec.nodes,
        "schedules": [s.canonical_dict() for s in schedules],
        "triggers": [_canonical_json_value(t) for t in triggers],
        "sandbox_pool": sandbox_pool,
        "collision_policy": str(collision_policy),
        "concurrency_cap": concurrency_cap,
        "pins": pins.canonical_dict(),
    }
    return json.dumps(_canonical_json_value(body), sort_keys=True, separators=(",", ":")).encode()


def compute_recipe_registration(
    spec: RecipeSpec,
    *,
    pins: RecipePins | None = None,
    collision_policy: CollisionPolicy | str = CollisionPolicy.CANCEL_NEW,
    concurrency_cap: int = 1,
    sandbox_pool: str = "",
) -> tuple[bytes, str, tuple[RecipeSchedule, ...], tuple[dict[str, Any], ...], str, CollisionPolicy, int, RecipePins]:
    """Resolve *spec* into its canonical bytes and content hash.

    Single source of truth shared by :meth:`RecipeRegistry.register` and the
    fleet planner so a planned hash and a registered hash agree byte-for-byte.
    """
    resolved_pins = pins if pins is not None else _pins_from_spec(spec)
    schedules = tuple(RecipeSchedule.from_mapping(s) for s in _spec_schedules(spec))
    triggers = tuple(_spec_triggers(spec))
    pool = sandbox_pool or _spec_sandbox_pool(spec)
    policy = CollisionPolicy(collision_policy)
    cap = max(1, concurrency_cap)
    canonical_bytes = canonical_recipe_bytes(
        spec=spec,
        pins=resolved_pins,
        schedules=schedules,
        collision_policy=policy,
        concurrency_cap=cap,
        sandbox_pool=pool,
        triggers=triggers,
    )
    digest = recipe_content_hash(canonical_bytes)
    return canonical_bytes, digest, schedules, triggers, pool, policy, cap, resolved_pins


def _canonical_json_value(value: Any) -> Any:
    """Recursively normalise *value* so equal structures serialise equal.

    Dicts sort by key; lists preserve order (node/schedule order is
    semantic). Scalars pass through. Guards against a manifest smuggling a
    non-JSON scalar into the hash by stringifying unknown types.
    """
    if isinstance(value, dict):
        mapping: dict[Any, Any] = value
        return {str(k): _canonical_json_value(mapping[k]) for k in sorted(mapping, key=str)}
    if isinstance(value, (list, tuple)):
        return [_canonical_json_value(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


@dataclass
class _NameState:
    """The projected lifecycle state of one recipe name."""

    live_hash: str = ""
    paused: bool = False
    last_receipt_hmac: str = ""
    receipts: list[dict[str, Any]] = field(default_factory=list)


class RecipeRegistry:
    """Content-addressed recipe registry projected from receipt history.

    The registry never holds a mutable row. Registration seals canonical
    bytes into the lineage spine and appends a receipt to the HMAC chain;
    the live ``name -> hash`` mapping and pause state are reconstructed by
    replaying the recipe lifecycle receipts. Content-addressed blobs of the
    canonical bytes are cached under ``.sdd/runtime/recipes/blobs`` so a
    fire can load the exact definition body by hash.
    """

    def __init__(
        self,
        sdd_dir: Path,
        *,
        chain: AuditChainStore | None = None,
        hmac_key: bytes | None = None,
        lineage_key: bytes | None = None,
    ) -> None:
        self._sdd_dir = sdd_dir
        self._dir = sdd_dir / "runtime" / "recipes"
        self._blobs = self._dir / "blobs"
        self._blobs.mkdir(parents=True, exist_ok=True)
        self._hmac_key = hmac_key
        self._lineage_key = lineage_key if lineage_key is not None else hmac_key
        self._chain = chain

    # -- chain access -------------------------------------------------------

    def _get_chain(self) -> AuditChainStore:
        if self._chain is not None:
            return self._chain
        from bernstein.core.security.audit_chain import AuditChainStore

        self._chain = AuditChainStore(self._sdd_dir / _AUDIT_SUBDIR, key=self._hmac_key)
        return self._chain

    def _lineage_hmac_key(self) -> bytes:
        if self._lineage_key is not None:
            return self._lineage_key
        from bernstein.core.security.audit import load_or_create_audit_key

        self._lineage_key = load_or_create_audit_key()
        return self._lineage_key

    # -- projection ---------------------------------------------------------

    def _lifecycle_events(self) -> list[dict[str, Any]]:
        """Return every recipe-lifecycle chain event in chain order."""
        from bernstein.core.security.audit_chain import (
            EVENT_RECIPE_PAUSE,
            EVENT_RECIPE_REGISTER,
            EVENT_RECIPE_RESUME,
            EVENT_RECIPE_ROLLBACK,
            EVENT_RECIPE_SUPERSEDE,
        )

        chain = self._get_chain()
        wanted = {
            EVENT_RECIPE_REGISTER,
            EVENT_RECIPE_SUPERSEDE,
            EVENT_RECIPE_ROLLBACK,
            EVENT_RECIPE_PAUSE,
            EVENT_RECIPE_RESUME,
        }
        out: list[dict[str, Any]] = []
        for event_type in sorted(wanted):
            for ev in chain.query(event_type=event_type):
                out.append(
                    {
                        "event_type": ev.event_type,
                        "hmac": getattr(ev, "hmac", ""),
                        "timestamp": getattr(ev, "timestamp", ""),
                        "details": dict(ev.details),
                    },
                )
        # Chain order is the write order; query() groups by type, so re-sort
        # by the embedded prev linkage is not enough. The underlying log
        # preserves per-type order, and lifecycle transitions for one name
        # are totally ordered by their prev_receipt_digest linkage, which is
        # what the projection and verify walk rely on - not this list order.
        return out

    def _project_name(self, name: str) -> _NameState:
        """Replay this name's receipts into its live state."""
        from bernstein.core.security.audit_chain import (
            EVENT_RECIPE_PAUSE,
            EVENT_RECIPE_REGISTER,
            EVENT_RECIPE_RESUME,
            EVENT_RECIPE_ROLLBACK,
            EVENT_RECIPE_SUPERSEDE,
        )

        events = [ev for ev in self._lifecycle_events() if str(ev["details"].get("name", "")) == name]
        # Order the name's receipts by their per-name lineage linkage: each
        # receipt names the hmac of its predecessor, so a linked list rebuild
        # is order-independent of how query() grouped them.
        ordered = _order_by_lineage(events)
        state = _NameState()
        for ev in ordered:
            details = ev["details"]
            etype = ev["event_type"]
            if etype in (EVENT_RECIPE_REGISTER, EVENT_RECIPE_SUPERSEDE):
                state.live_hash = str(details.get("recipe_hash") or details.get("new_hash") or "")
            elif etype == EVENT_RECIPE_ROLLBACK:
                state.live_hash = str(details.get("to_hash", ""))
            elif etype == EVENT_RECIPE_PAUSE:
                state.paused = True
            elif etype == EVENT_RECIPE_RESUME:
                state.paused = False
            state.last_receipt_hmac = str(ev["hmac"])
            state.receipts.append(ev)
        return state

    def live_hash(self, name: str) -> str | None:
        """Return the live recipe hash for *name*, or None if never registered."""
        state = self._project_name(name)
        return state.live_hash or None

    def is_paused(self, name: str) -> bool:
        """Return whether *name* is currently paused (fires nothing)."""
        return self._project_name(name).paused

    def history(self, name: str) -> list[dict[str, Any]]:
        """Return the name's lifecycle receipts in definition-lineage order."""
        return self._project_name(name).receipts.copy()

    # -- blobs --------------------------------------------------------------

    def _blob_path(self, recipe_hash: str) -> Path:
        return self._blobs / f"{recipe_hash}.json"

    def get_canonical_bytes(self, recipe_hash: str) -> bytes | None:
        """Load the content-addressed canonical bytes for *recipe_hash*."""
        path = self._blob_path(recipe_hash)
        if not path.exists():
            return None
        data = path.read_bytes()
        # Verify the blob still hashes to its name - a tampered blob must not
        # masquerade as the definition.
        if recipe_content_hash(data) != recipe_hash:
            return None
        return data

    # -- lifecycle ----------------------------------------------------------

    def register(
        self,
        *,
        spec: RecipeSpec,
        pins: RecipePins | None = None,
        collision_policy: CollisionPolicy | str = CollisionPolicy.CANCEL_NEW,
        concurrency_cap: int = 1,
        sandbox_pool: str = "",
        actor: str = "operator",
        now: float | None = None,
    ) -> RegisteredRecipe:
        """Register *spec* under its name; seal, hash, and receipt it.

        Idempotent: registering a byte-identical definition returns the
        existing registration without a new receipt. Registering a changed
        body under a live name writes an operator-signed supersede receipt.
        """
        (
            canonical_bytes,
            digest,
            schedules,
            _triggers,
            _pool,
            policy,
            cap,
            resolved_pins,
        ) = compute_recipe_registration(
            spec,
            pins=pins,
            collision_policy=collision_policy,
            concurrency_cap=concurrency_cap,
            sandbox_pool=sandbox_pool,
        )

        state = self._project_name(spec.name)
        if state.live_hash == digest:
            existing = self._load_registered(spec.name, digest)
            if existing is not None:
                return existing

        # Seal canonical bytes into the lineage spine (ungated, deterministic).
        spine_anchor = self._seal(canonical_bytes, digest)
        self._blob_path(digest).write_bytes(canonical_bytes)

        chain = self._get_chain()
        from bernstein.core.security.audit_chain import (
            record_recipe_register,
            record_recipe_supersede,
        )

        prev_receipt = state.last_receipt_hmac
        superseded = ""
        if state.live_hash and state.live_hash != digest:
            superseded = state.live_hash
            record_recipe_supersede(
                chain=chain,
                name=spec.name,
                old_hash=state.live_hash,
                new_hash=digest,
                spine_anchor=spine_anchor,
                prev_receipt_digest=prev_receipt,
                actor=actor,
            )
        else:
            record_recipe_register(
                chain=chain,
                name=spec.name,
                recipe_hash=digest,
                spine_anchor=spine_anchor,
                prev_receipt_digest=prev_receipt,
                actor=actor,
            )

        return RegisteredRecipe(
            name=spec.name,
            recipe_hash=digest,
            canonical_bytes=canonical_bytes,
            spine_anchor=spine_anchor,
            schedules=schedules,
            collision_policy=policy,
            concurrency_cap=cap,
            pins=resolved_pins,
            superseded_hash=superseded,
            registered_at=float(now) if now is not None else time.time(),
        )

    def rollback(self, name: str, target_hash: str, *, actor: str = "operator") -> str:
        """Re-point *name* at a prior *target_hash* via a rollback receipt.

        Nothing is deleted; the rollback is itself a chain record.

        Raises:
            RecipeRegistryError: When the name has no live hash or the
                target hash was never registered under the name.
        """
        state = self._project_name(name)
        if not state.live_hash:
            raise RecipeRegistryError(f"recipe {name!r} is not registered")
        if self.get_canonical_bytes(target_hash) is None:
            raise RecipeRegistryError(f"target hash {target_hash!r} is not a known definition of {name!r}")
        from bernstein.core.security.audit_chain import record_recipe_rollback

        record_recipe_rollback(
            chain=self._get_chain(),
            name=name,
            from_hash=state.live_hash,
            to_hash=target_hash,
            prev_receipt_digest=state.last_receipt_hmac,
            actor=actor,
        )
        return target_hash

    def pause(self, name: str, *, actor: str = "operator") -> None:
        """Pause *name*: future fires stop, identity and history are kept."""
        self._set_pause(name, paused=True, actor=actor)

    def resume(self, name: str, *, actor: str = "operator") -> None:
        """Resume a paused *name*."""
        self._set_pause(name, paused=False, actor=actor)

    def _set_pause(self, name: str, *, paused: bool, actor: str) -> None:
        state = self._project_name(name)
        if not state.live_hash:
            raise RecipeRegistryError(f"recipe {name!r} is not registered")
        from bernstein.core.security.audit_chain import record_recipe_pause

        record_recipe_pause(
            chain=self._get_chain(),
            name=name,
            recipe_hash=state.live_hash,
            paused=paused,
            prev_receipt_digest=state.last_receipt_hmac,
            actor=actor,
        )

    def fire(
        self,
        name: str,
        *,
        fire_time: int,
        goal: str = "",
    ) -> RecipeFireResult:
        """Fire *name* as a deterministic projection; refuse when paused.

        A paused recipe fires nothing (AC6). Otherwise the fire is a pure
        projection of ``(recipe_hash, fire_time)`` with the declared
        timezone folded in, returning the projection hash as the response.
        """
        state = self._project_name(name)
        if not state.live_hash:
            raise RecipeRegistryError(f"recipe {name!r} is not registered")
        if state.paused:
            return RecipeFireResult(
                name=name,
                recipe_hash=state.live_hash,
                dispatched=False,
                fire_time=fire_time,
                reason="recipe is paused",
            )
        from bernstein.core.orchestration.schedule_projection import project_schedule_fire

        schedules = self._registered_schedules(name, state.live_hash)
        tz = schedules[0].timezone if schedules else ""
        dst = schedules[0].dst_policy if schedules else ""
        recurrence = schedules[0].recurrence if schedules else ""
        projection = project_schedule_fire(
            schedule_id=state.live_hash,
            fire_time=fire_time,
            last_state=None,
            goal=goal,
            recurrence=recurrence,
            timezone=tz,
            dst_policy=dst if tz else "",
        )
        return RecipeFireResult(
            name=name,
            recipe_hash=state.live_hash,
            dispatched=True,
            fire_time=fire_time,
            projection_hash=projection.projection_hash,
            chain_anchor=self._get_chain().prev_chain_digest,
        )

    # -- verification -------------------------------------------------------

    def verify_history(self, name: str) -> tuple[bool, list[str]]:
        """Walk *name*'s receipts against the HMAC chain offline (AC5).

        Returns ``(ok, errors)``. Fails (non-empty errors) when the audit
        chain itself does not verify (any byte mutation, deleted, or
        reordered line) or when the name's per-receipt lineage linkage is
        broken. No server is required: the chain is read from its on-disk
        JSONL segments.
        """
        errors: list[str] = []
        chain = self._get_chain()
        chain_ok, chain_errors = chain.verify()
        if not chain_ok:
            errors.extend(chain_errors)

        state = self._project_name(name)
        if not state.receipts:
            errors.append(f"recipe {name!r} has no lifecycle receipts")
            return (not errors, errors)

        prev = ""
        for ev in state.receipts:
            linked = str(ev["details"].get("prev_receipt_digest", ""))
            if linked != prev:
                errors.append(
                    f"broken definition-lineage link for {name!r}: receipt expected prev {prev[:16] or '(genesis)'} "
                    f"but names {linked[:16] or '(genesis)'}",
                )
                break
            prev = str(ev["hmac"])
        return (not errors, errors)

    # -- internals ----------------------------------------------------------

    def _seal(self, canonical_bytes: bytes, recipe_hash: str) -> str:
        """Seal canonical bytes into the lineage spine, return the entry hash."""
        from bernstein.core.lineage.spine import LineageSpine

        return LineageSpine(
            self._sdd_dir / _LINEAGE_SUBDIR,
            run_id=recipe_run_id(recipe_hash),
            hmac_key=self._lineage_hmac_key(),
        ).record(
            artifact_path=f".sdd/runtime/recipes/blobs/{recipe_hash}.json",
            content=canonical_bytes,
            actor="recipe_registry",
            step_id=f"recipe-register:{recipe_hash[:16]}",
            model="",
            timestamp=0,
        )

    def _registered_schedules(self, name: str, recipe_hash: str) -> tuple[RecipeSchedule, ...]:
        """Recover the schedules of the live definition from its blob."""
        data = self.get_canonical_bytes(recipe_hash)
        if data is None:
            return ()
        try:
            body = json.loads(data)
        except json.JSONDecodeError:
            return ()
        out: list[RecipeSchedule] = []
        for raw in body.get("schedules", []):
            try:
                out.append(RecipeSchedule.from_mapping(raw))
            except ScheduleKindError:
                continue
        return tuple(out)

    def _load_registered(self, name: str, recipe_hash: str) -> RegisteredRecipe | None:
        """Rebuild a RegisteredRecipe view from the content-addressed blob."""
        data = self.get_canonical_bytes(recipe_hash)
        if data is None:
            return None
        try:
            body = json.loads(data)
        except json.JSONDecodeError:
            return None
        pins_raw = body.get("pins", {})
        pins = RecipePins(
            git_commit=str(pins_raw.get("git_commit", "")),
            adapter=str(pins_raw.get("adapter", "")),
            model=str(pins_raw.get("model", "")),
            prompt_pack_sha256=str(pins_raw.get("prompt_pack_sha256", "")),
        )
        schedules = self._registered_schedules(name, recipe_hash)
        return RegisteredRecipe(
            name=name,
            recipe_hash=recipe_hash,
            canonical_bytes=data,
            spine_anchor="",
            schedules=schedules,
            collision_policy=CollisionPolicy(str(body.get("collision_policy", CollisionPolicy.CANCEL_NEW))),
            concurrency_cap=int(body.get("concurrency_cap", 1)),
            pins=pins,
        )


def _order_by_lineage(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Order a name's receipts by their prev_receipt_digest linkage.

    Each receipt names the hmac of its predecessor (``""`` for genesis).
    Rebuilding the linked list makes the projection independent of how the
    chain query grouped events by type. On a broken or forked linkage the
    reachable prefix is returned (verify_history reports the break).
    """
    by_prev: dict[str, dict[str, Any]] = {}
    for ev in events:
        prev = str(ev["details"].get("prev_receipt_digest", ""))
        by_prev.setdefault(prev, ev)
    ordered: list[dict[str, Any]] = []
    cursor = ""
    seen: set[str] = set()
    while cursor in by_prev:
        ev = by_prev[cursor]
        hmac = str(ev["hmac"])
        if hmac in seen:
            break
        seen.add(hmac)
        ordered.append(ev)
        cursor = hmac
    # Fall back to input order for any receipts not reachable through the
    # linkage (e.g. a genesis-less legacy receipt), so nothing is dropped.
    if len(ordered) != len(events):
        reached = {str(e["hmac"]) for e in ordered}
        ordered.extend(e for e in events if str(e["hmac"]) not in reached)
    return ordered


def _pins_from_spec(spec: RecipeSpec) -> RecipePins:
    """Build pins from the manifest ``pins:`` block, defaulting the commit.

    An explicit ``git_commit`` in the manifest wins (reproducible off a
    checkout); otherwise the current HEAD is resolved so a live registration
    still binds the source revision.
    """
    raw: dict[str, Any] = dict(getattr(spec, "pins", {}) or {})
    return RecipePins(
        git_commit=str(raw.get("git_commit", "") or current_git_commit()),
        adapter=str(raw.get("adapter", "")),
        model=str(raw.get("model", "")),
        prompt_pack_sha256=str(raw.get("prompt_pack_sha256", "")),
    )


def _spec_schedules(spec: RecipeSpec) -> list[dict[str, Any]]:
    return list(getattr(spec, "schedules", []) or [])


def _spec_triggers(spec: RecipeSpec) -> list[dict[str, Any]]:
    return list(getattr(spec, "triggers", []) or [])


def _spec_sandbox_pool(spec: RecipeSpec) -> str:
    return str(getattr(spec, "sandbox_pool", "") or "")
