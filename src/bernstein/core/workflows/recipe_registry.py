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
import logging
import subprocess
import threading
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
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

logger = logging.getLogger(__name__)

__all__ = [
    "CANONICAL_RECIPE_REV",
    "RecipeDispatch",
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

#: Task-graph dispatcher for a recipe fire. Receives the normalised trigger
#: event and returns **the identifiers of the work items it submitted and the
#: sink accepted** - for example the task ids the task server returned.
#:
#: Identifiers, not a count, because the fire receipt is an HMAC-chained claim
#: that work happened: recording *which* work lets an auditor go and check it
#: exists, where a bare number can only be taken on trust. A dispatcher that
#: merely *renders* or *queues* candidate work has not submitted anything and
#: must return an empty sequence; ``fire`` then reports failure and writes no
#: receipt rather than attesting a dispatch it cannot back up.
RecipeDispatch = Callable[[Any], "Sequence[str]"]

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

    @property
    def schedule_id(self) -> str:
        """Content-derived identity of this schedule, ``sched_<12hex>``.

        Derived from the canonical fields alone, so the id is stable across
        hosts and processes and two distinct schedules on one recipe get
        distinct ids. A fire names the schedule that triggered it by this
        id, which is what keeps a multi-schedule recipe from projecting
        every fire under one of its schedules.
        """
        payload = json.dumps(self.canonical_dict(), sort_keys=True, separators=(",", ":")).encode()
        return "sched_" + hashlib.sha256(payload).hexdigest()[:12]

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

    ``dispatched`` is True only when the task-graph dispatcher reported
    submitted work *and* a ``recipe.fire`` receipt was appended; every other
    outcome (paused, no dispatcher, dispatcher error, nothing submitted) is
    False and carries a ``reason``.

    ``projection_hash`` is the deterministic fire projection hash and
    ``chain_anchor`` is the hmac of the fire receipt - the fire is a hash
    rather than an opaque job id.

    Attributes:
        schedule_id: Content-derived id of the schedule that triggered the
            fire, or ``""`` for a schedule-neutral manual fire.
        submitted: Number of work items the sink accepted.
        submitted_ids: Identifiers of those work items, in submission order.
        paused: True only when the recipe was paused. Structured state, so a
            caller decides between "deliberately not fired" and "failed to
            fire" from a field rather than by parsing ``reason``, which is
            free-form prose derived from arbitrary dispatcher errors.
    """

    name: str
    recipe_hash: str
    dispatched: bool
    fire_time: int
    projection_hash: str = ""
    chain_anchor: str = ""
    reason: str = ""
    schedule_id: str = ""
    submitted: int = 0
    submitted_ids: tuple[str, ...] = ()
    paused: bool = False


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

    Non-string dict keys are rejected rather than coerced. Coercing them
    would let two distinct keys (``1`` and ``"1"``, ``True`` and ``"True"``)
    stringify onto the same entry, silently dropping one value and folding
    two different definitions into one hash - the exact failure the
    content-addressing exists to make impossible.

    Raises:
        RecipeRegistryError: When any nested mapping has a non-string key.
    """
    if isinstance(value, dict):
        mapping: dict[Any, Any] = value
        offending = [k for k in mapping if not isinstance(k, str)]
        if offending:
            rendered = ", ".join(sorted(repr(k) for k in offending))
            raise RecipeRegistryError(
                f"canonical recipe body requires string keys; got non-string key(s) {rendered}",
            )
        return {k: _canonical_json_value(mapping[k]) for k in sorted(mapping)}
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


@dataclass(frozen=True)
class _StagedRegistration:
    """A registration whose fallible work is done but whose receipt is not written.

    ``existing`` is set when the definition is already live under the name
    (idempotent re-registration): committing it is a no-op. Otherwise
    ``recipe`` carries the sealed registration and ``prev_receipt_digest``
    the lineage predecessor its receipt will link to.
    """

    recipe: RegisteredRecipe | None = None
    existing: RegisteredRecipe | None = None
    prev_receipt_digest: str = ""


def _submitted_identifiers(returned: Any) -> tuple[str, ...] | None:
    """Coerce a dispatcher return value into work-item identifiers.

    Returns the identifiers, ``()`` when the dispatcher reported submitting
    nothing, or ``None`` when the value is not an identifier sequence at all
    (the dispatcher does not honour the contract, which is distinct from
    honestly reporting no work).

    Deliberately strict. A bare ``int`` is a count with no evidence behind
    it; ``True`` is an ``int`` subclass and would otherwise read as one item;
    a bare ``str`` is iterable and would silently decompose into one
    "identifier" per character.
    """
    if isinstance(returned, (str, bytes)) or not isinstance(returned, Sequence):
        return None
    identifiers: list[str] = []
    for item in returned:
        if not isinstance(item, str) or not item.strip():
            return None
        identifiers.append(item)
    return tuple(identifiers)


def _lineage_hashes(receipts: list[dict[str, Any]]) -> set[str]:
    """Return every definition hash named by a name's own lifecycle receipts."""
    fields = ("recipe_hash", "new_hash", "old_hash", "to_hash", "from_hash")
    known: set[str] = set()
    for ev in receipts:
        details = ev.get("details", {})
        known.update(str(details[key]) for key in fields if details.get(key))
    return known


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
        dispatch: RecipeDispatch | None = None,
    ) -> None:
        self._sdd_dir = sdd_dir
        self._dir = sdd_dir / "runtime" / "recipes"
        self._blobs = self._dir / "blobs"
        self._blobs.mkdir(parents=True, exist_ok=True)
        self._hmac_key = hmac_key
        self._lineage_key = lineage_key if lineage_key is not None else hmac_key
        self._chain = chain
        # Task-graph dispatcher invoked by ``fire``. There is no fallback: a
        # registry with none wired cannot dispatch, and says so, rather than
        # improvising a substitute whose return value would not evidence a
        # submission.
        self._dispatch = dispatch
        # Nested write_lock() depth, per thread (see write_lock).
        self._lock_state = threading.local()
        self._thread_lock = threading.Lock()

    # -- write serialisation ------------------------------------------------

    @property
    def _lock_path(self) -> Path:
        return self._dir / "locks" / "registry.lock"

    @contextmanager
    def write_lock(self) -> Iterator[None]:
        """Hold an exclusive cross-process lock over registry writes.

        Every mutation that must be seen as one step - a fleet apply's
        base-state recheck, its registrations, and its aggregate receipt -
        is held inside a single acquisition, so a concurrent writer cannot
        interleave a registration between the recheck and the receipt.

        Re-entrant within one registry instance. The underlying ``flock`` is
        per file descriptor, so a nested acquisition from the same process
        would open a second descriptor and block on a lock it already holds -
        a deadlock with no error and no timeout, just a hung process still
        holding the lock. Counting the depth turns that footgun into ordinary
        nesting, which matters because the obvious way to extend
        :func:`~bernstein.core.workflows.recipe_fleet.apply_fleet` is to call
        a locking method from inside the locked section.

        Not re-entrant *across* instances or processes, which is the point:
        two registries over the same ``.sdd`` still serialise.
        """
        # Depth is per thread. Instance state would let a second thread see
        # depth>0 while the first holds the flock, skip acquisition entirely,
        # and run unserialised - silently losing the exclusion the lock exists
        # to provide. The threading.Lock serialises threads of this process;
        # the flock serialises processes.
        if getattr(self._lock_state, "depth", 0) > 0:
            self._lock_state.depth += 1
            try:
                yield
            finally:
                self._lock_state.depth -= 1
            return

        from bernstein.core.persistence.file_locks import cross_process_lock

        with self._thread_lock, cross_process_lock(self._lock_path):
            self._lock_state.depth = 1
            try:
                yield
            finally:
                self._lock_state.depth = 0

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
        """Replay this name's receipts into its live state.

        Raises:
            RecipeRegistryError: When the name's receipt lineage is forked,
                orphaned, or cyclic. The projection fails closed rather than
                serving a live hash derived from a history that does not
                reconstruct.
        """
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
        ordered = _order_by_lineage(events, name, self._lineage_resolutions(name))
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

    def _lineage_resolutions(self, name: str) -> dict[str, str]:
        """Return ``predecessor -> chosen successor`` from resolution receipts.

        The latest resolution for a predecessor wins, so a mistaken repair is
        corrected by another repair rather than by editing the chain.
        """
        from bernstein.core.security.audit_chain import EVENT_RECIPE_LINEAGE_RESOLVE

        out: dict[str, str] = {}
        for ev in self._get_chain().query(event_type=EVENT_RECIPE_LINEAGE_RESOLVE):
            details = dict(ev.details)
            if str(details.get("name", "")) != name:
                continue
            out[str(details.get("predecessor", ""))] = str(details.get("chosen_receipt", ""))
        return out

    def lineage_forks(self, name: str) -> dict[str, list[dict[str, Any]]]:
        """Return unresolved ``predecessor -> competing receipts`` for *name*.

        The inspection half of the recovery path: an operator has to see the
        competing branches before choosing one, and ``history`` cannot show
        them because the projection it feeds fails closed on the fork.
        """
        events = [ev for ev in self._lifecycle_events() if str(ev["details"].get("name", "")) == name]
        by_prev: dict[str, list[dict[str, Any]]] = {}
        for ev in events:
            by_prev.setdefault(str(ev["details"].get("prev_receipt_digest", "")), []).append(ev)
        resolved = self._lineage_resolutions(name)
        return {
            prev: evs
            for prev, evs in by_prev.items()
            if len(evs) > 1 and resolved.get(prev, "") not in {str(e["hmac"]) for e in evs}
        }

    def repair_lineage(self, name: str, chosen_receipt: str, *, actor: str = "operator") -> str:
        """Resolve a forked lineage by naming the successor to follow.

        Recovery, not repair in the destructive sense: nothing is deleted, the
        losing branch stays on the chain, and the decision is itself a
        receipt. Without this a fork produced by an ordinary concurrent write
        would leave the name permanently unusable, since every operation
        projects the lineage first and an append-only chain cannot be edited.

        Args:
            name: Recipe name with a forked lineage.
            chosen_receipt: Full or 16-char-prefixed hmac of the successor to
                follow.
            actor: Operator performing the resolution.

        Returns:
            The full hmac of the chosen receipt.

        Raises:
            RecipeRegistryError: When the name has no unresolved fork, or the
                chosen receipt is not one of the competing successors.
        """
        with self.write_lock():
            forks = self.lineage_forks(name)
            if not forks:
                raise RecipeRegistryError(f"recipe {name!r} has no unresolved definition-lineage fork")
            wanted = chosen_receipt.strip()
            for predecessor, candidates in sorted(forks.items()):
                for candidate in candidates:
                    hmac = str(candidate["hmac"])
                    if hmac == wanted or hmac.startswith(wanted):
                        from bernstein.core.security.audit_chain import record_recipe_lineage_resolve

                        record_recipe_lineage_resolve(
                            chain=self._get_chain(),
                            name=name,
                            predecessor=predecessor,
                            chosen_receipt=hmac,
                            superseded_receipts=tuple(
                                str(other["hmac"]) for other in candidates if str(other["hmac"]) != hmac
                            ),
                            actor=actor,
                        )
                        return hmac
            available = ", ".join(
                sorted(str(c["hmac"])[:16] for candidates in forks.values() for c in candidates),
            )
            raise RecipeRegistryError(
                f"{chosen_receipt!r} is not a competing successor for {name!r}; candidates: {available}",
            )

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
        with self.write_lock():
            staged = self.prepare_registration(
                spec=spec,
                pins=pins,
                collision_policy=collision_policy,
                concurrency_cap=concurrency_cap,
                sandbox_pool=sandbox_pool,
                now=now,
            )
            return self.commit_registration(staged, actor=actor)

    def prepare_registration(
        self,
        *,
        spec: RecipeSpec,
        pins: RecipePins | None = None,
        collision_policy: CollisionPolicy | str = CollisionPolicy.CANCEL_NEW,
        concurrency_cap: int = 1,
        sandbox_pool: str = "",
        now: float | None = None,
    ) -> _StagedRegistration:
        """Do every fallible part of a registration without touching the chain.

        Canonicalisation, the lineage seal, and the blob write all happen
        here; :meth:`commit_registration` then only appends the receipt.
        Splitting the two is what lets a multi-recipe apply be all-or-nothing:
        the live ``name -> hash`` mapping is projected from chain receipts
        alone, so a preparation that fails part-way leaves content-addressed
        bytes on disk that no receipt points at - inert, not half-registered.
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
                return _StagedRegistration(existing=existing)

        # Seal canonical bytes into the lineage spine (ungated, deterministic).
        spine_anchor = self._seal(canonical_bytes, digest)
        self._blob_path(digest).write_bytes(canonical_bytes)
        return _StagedRegistration(
            recipe=RegisteredRecipe(
                name=spec.name,
                recipe_hash=digest,
                canonical_bytes=canonical_bytes,
                spine_anchor=spine_anchor,
                schedules=schedules,
                collision_policy=policy,
                concurrency_cap=cap,
                pins=resolved_pins,
                superseded_hash=state.live_hash if state.live_hash and state.live_hash != digest else "",
                registered_at=float(now) if now is not None else time.time(),
            ),
            prev_receipt_digest=state.last_receipt_hmac,
        )

    def commit_registration(self, staged: _StagedRegistration, *, actor: str = "operator") -> RegisteredRecipe:
        """Append the lifecycle receipt for a prepared registration."""
        if staged.existing is not None:
            return staged.existing
        recipe = staged.recipe
        if recipe is None:  # pragma: no cover - defensive
            raise RecipeRegistryError("staged registration carries neither an existing nor a new recipe")

        chain = self._get_chain()
        from bernstein.core.security.audit_chain import (
            record_recipe_register,
            record_recipe_supersede,
        )

        if recipe.superseded_hash:
            record_recipe_supersede(
                chain=chain,
                name=recipe.name,
                old_hash=recipe.superseded_hash,
                new_hash=recipe.recipe_hash,
                spine_anchor=recipe.spine_anchor,
                prev_receipt_digest=staged.prev_receipt_digest,
                actor=actor,
            )
        else:
            record_recipe_register(
                chain=chain,
                name=recipe.name,
                recipe_hash=recipe.recipe_hash,
                spine_anchor=recipe.spine_anchor,
                prev_receipt_digest=staged.prev_receipt_digest,
                actor=actor,
            )
        return recipe

    def rollback(self, name: str, target_hash: str, *, actor: str = "operator") -> str:
        """Re-point *name* at a prior *target_hash* via a rollback receipt.

        Nothing is deleted; the rollback is itself a chain record.

        The target must appear in *this name's own* lifecycle receipts. The
        blob store is global and content-addressed, so a hash being present
        on disk only proves some recipe once had that body - rolling a name
        onto another recipe's definition would re-point it at a body it
        never ran, and the rollback receipt would attest to a lineage that
        does not exist.

        Raises:
            RecipeRegistryError: When the name has no live hash, when the
                target hash is absent from the name's own lineage, or when
                its canonical bytes are missing from the blob store.
        """
        with self.write_lock():
            return self._rollback_locked(name, target_hash, actor=actor)

    def _rollback_locked(self, name: str, target_hash: str, *, actor: str) -> str:
        state = self._project_name(name)
        if not state.live_hash:
            raise RecipeRegistryError(f"recipe {name!r} is not registered")
        known = _lineage_hashes(state.receipts)
        if target_hash not in known:
            raise RecipeRegistryError(
                f"target hash {target_hash[:16]!r} does not appear in the definition lineage of {name!r}; "
                f"rollback is confined to hashes this name itself registered",
            )
        if self.get_canonical_bytes(target_hash) is None:
            raise RecipeRegistryError(f"target hash {target_hash!r} has no canonical bytes on disk")
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
        # Under the same lock register() takes. Reading the lineage tail and
        # appending against it must be one step: interleaving a concurrent
        # write between them forks the lineage, and a fork costs the operator
        # a manual repair.
        with self.write_lock():
            self._set_pause_locked(name, paused=paused, actor=actor)

    def _set_pause_locked(self, name: str, *, paused: bool, actor: str) -> None:
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

    def declared_schedules(self, name: str) -> dict[str, RecipeSchedule]:
        """Return the live definition's schedules keyed by content-derived id.

        The ids are what :meth:`fire` accepts to attribute a fire to the
        schedule that triggered it.
        """
        state = self._project_name(name)
        if not state.live_hash:
            raise RecipeRegistryError(f"recipe {name!r} is not registered")
        return {s.schedule_id: s for s in self._registered_schedules(name, state.live_hash)}

    def fire(
        self,
        name: str,
        *,
        fire_time: int,
        goal: str = "",
        schedule_id: str = "",
        dispatch: RecipeDispatch | None = None,
    ) -> RecipeFireResult:
        """Fire *name*: submit the task graph, then receipt what was submitted.

        A paused recipe fires nothing (AC6). Otherwise the fire is a pure
        projection of ``(recipe_hash, fire_time)``, handed to the task-graph
        dispatcher and - only if the dispatcher reports submitted work -
        anchored with a ``recipe.fire`` receipt on the audit chain.

        ``dispatched=True`` is therefore a claim backed by two facts: work
        was submitted and a receipt records it. A dispatcher that raises,
        that is unavailable, or that submits nothing yields
        ``dispatched=False`` with a reason and appends no receipt, so the
        chain never carries a fire that did not happen.

        Schedule attribution: ``schedule_id`` names which declared schedule
        triggered this fire, and its recurrence / timezone / DST policy are
        folded into the projection. A manual fire leaves it empty and stays
        schedule-neutral rather than borrowing the semantics of whichever
        schedule happens to be declared first.

        Args:
            name: Registered recipe name.
            fire_time: Unix epoch of the fire instant.
            goal: Free-text goal folded into the projection.
            schedule_id: Content-derived id of the triggering schedule (see
                :meth:`declared_schedules`), or ``""`` for a manual fire.
            dispatch: Per-call dispatcher override; defaults to the one the
                registry was constructed with, then to the trigger pipeline.

        Raises:
            RecipeRegistryError: When the name is unregistered or
                ``schedule_id`` names no schedule of the live definition.
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
                paused=True,
            )
        from bernstein.core.orchestration.schedule_projection import project_schedule_fire

        schedule = self._resolve_schedule(name, state.live_hash, schedule_id)
        projection = project_schedule_fire(
            schedule_id=state.live_hash,
            fire_time=fire_time,
            last_state=None,
            goal=goal,
            recurrence=schedule.recurrence if schedule is not None else "",
            timezone=schedule.timezone if schedule is not None else "",
            dst_policy=(schedule.dst_policy if schedule is not None and schedule.timezone else ""),
        )

        submitted_ids, reason = self._submit(
            name=name,
            recipe_hash=state.live_hash,
            fire_time=fire_time,
            goal=goal,
            schedule_id=schedule_id,
            projection_hash=projection.projection_hash,
            dispatch=dispatch,
        )
        if not submitted_ids:
            return RecipeFireResult(
                name=name,
                recipe_hash=state.live_hash,
                dispatched=False,
                fire_time=fire_time,
                projection_hash=projection.projection_hash,
                schedule_id=schedule_id,
                reason=reason or "the dispatcher submitted no work",
            )

        from bernstein.core.security.audit_chain import record_recipe_fire

        event = record_recipe_fire(
            chain=self._get_chain(),
            name=name,
            recipe_hash=state.live_hash,
            fire_time=fire_time,
            projection_hash=projection.projection_hash,
            schedule_id=schedule_id,
            submitted_ids=submitted_ids,
        )
        return RecipeFireResult(
            name=name,
            recipe_hash=state.live_hash,
            dispatched=True,
            fire_time=fire_time,
            projection_hash=projection.projection_hash,
            chain_anchor=str(getattr(event, "hmac", "")),
            schedule_id=schedule_id,
            submitted=len(submitted_ids),
            submitted_ids=submitted_ids,
        )

    def _resolve_schedule(self, name: str, recipe_hash: str, schedule_id: str) -> RecipeSchedule | None:
        """Return the declared schedule *schedule_id* names, or None if manual.

        Raises:
            RecipeRegistryError: When *schedule_id* is non-empty but names
                no schedule of the live definition. Falling back to a
                default schedule here would attribute the fire to a
                recurrence the caller did not ask for.
        """
        if not schedule_id:
            return None
        declared = {s.schedule_id: s for s in self._registered_schedules(name, recipe_hash)}
        schedule = declared.get(schedule_id)
        if schedule is None:
            known = ", ".join(sorted(declared)) or "(none declared)"
            raise RecipeRegistryError(
                f"recipe {name!r} declares no schedule {schedule_id!r}; declared schedules: {known}",
            )
        return schedule

    def _submit(
        self,
        *,
        name: str,
        recipe_hash: str,
        fire_time: int,
        goal: str,
        schedule_id: str,
        projection_hash: str,
        dispatch: RecipeDispatch | None,
    ) -> tuple[tuple[str, ...], str]:
        """Hand the fire to the dispatcher; return ``(submitted_ids, reason)``.

        An empty tuple always carries a non-empty reason. Every failure mode -
        no dispatcher wired, the dispatcher raising, or the dispatcher
        returning nothing that identifies accepted work - lands here rather
        than being swallowed, because the caller uses this result to decide
        whether it may write a receipt claiming the fire happened.

        There is deliberately no fallback dispatcher. Synthesising one from
        whatever component happens to be reachable is how a fire ends up
        counting *candidate* work (a rendered payload, a matched rule) as
        submitted work, which is exactly the false attestation the receipt
        exists to rule out. No wiring means no dispatch, and no dispatch
        means no receipt.
        """
        from bernstein.core.trigger_sources.schedule import normalize_schedule_fire

        dispatcher = dispatch if dispatch is not None else self._dispatch
        if dispatcher is None:
            return (), "no task-graph dispatcher is configured for this registry; nothing was submitted"

        event = normalize_schedule_fire(
            schedule_id=recipe_hash,
            fire_time=float(fire_time),
            goal=goal,
            projection_hash=projection_hash,
            extra={
                "recipe_name": name,
                "recipe_hash": recipe_hash,
                "recipe_schedule_id": schedule_id,
            },
        )
        try:
            returned = dispatcher(event)
        except Exception as exc:
            return (), f"dispatch failed: {exc}"

        submitted_ids = _submitted_identifiers(returned)
        if submitted_ids is None:
            return (), (
                "the task-graph dispatcher did not return work-item identifiers; "
                "a fire is only recorded against work the sink accepted"
            )
        if not submitted_ids:
            return (), "the task-graph dispatcher submitted no work for this fire"
        return submitted_ids, ""

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

        try:
            state = self._project_name(name)
        except RecipeRegistryError as exc:
            # The lineage does not reconstruct. Report it rather than
            # raising: ``history --verify`` exists to name this failure.
            errors.append(str(exc))
            return (False, errors)
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


def _order_by_lineage(
    events: list[dict[str, Any]],
    name: str = "",
    resolutions: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Order a name's receipts by their prev_receipt_digest linkage.

    Each receipt names the hmac of its predecessor (``""`` for genesis).
    Rebuilding the linked list makes the projection independent of how the
    chain query grouped events by type.

    Fails closed. A definition lineage is a chain, so every anomaly here is
    evidence that the receipt history is not what it claims to be:

    - two receipts naming the same predecessor is a **fork**: the name has
      two competing successors and there is no honest way to pick one, so
      picking the first-seen silently resolves a conflict in favour of
      whatever order the chain query happened to return;
    - a receipt whose predecessor is unreachable is an **orphan**: its
      position in the lineage is unproven, so appending it to the tail
      fabricates an ordering the chain never attested;
    - a cycle is impossible in an append-only chain and means tampering.

    Every one of those raises rather than degrading to a best guess.

    A fork is recoverable rather than terminal: an operator resolves it with
    ``recipes repair-lineage``, which appends a receipt naming the successor
    to follow. *resolutions* carries those decisions, so a resolved fork
    projects normally while the losing branch stays on the chain and in
    ``history``. Without a resolution the fork still fails closed.

    Args:
        events: The name's lifecycle receipts, in any order.
        name: Recipe name, used only for the error message.
        resolutions: ``predecessor -> chosen successor hmac`` from operator
            resolution receipts.

    Returns:
        The receipts in definition-lineage order.

    Raises:
        RecipeRegistryError: On an unresolved fork, or a cyclic lineage.
    """
    label = f"recipe {name!r}" if name else "recipe"
    resolved = resolutions or {}
    by_prev: dict[str, list[dict[str, Any]]] = {}
    for ev in events:
        prev = str(ev["details"].get("prev_receipt_digest", ""))
        by_prev.setdefault(prev, []).append(ev)

    forked = {prev: evs for prev, evs in by_prev.items() if len(evs) > 1}
    for prev, evs in forked.items():
        chosen = resolved.get(prev, "")
        picked = [e for e in evs if str(e["hmac"]) == chosen]
        if not picked:
            successors = ", ".join(sorted(str(e["hmac"])[:16] for e in evs))
            hint = " ".join(
                f"bernstein recipes repair-lineage {name or '<name>'} --pick {str(e['hmac'])[:16]}" for e in evs[:1]
            )
            raise RecipeRegistryError(
                f"forked definition lineage for {label}: predecessor "
                f"{prev[:16] or '(genesis)'} has {len(evs)} successors ({successors}). "
                f"Nothing is lost - pick the branch to follow with: {hint}",
            )
        # Follow the operator-chosen successor; the others stay on the chain
        # and in history, they are simply not walked.
        by_prev[prev] = picked

    ordered: list[dict[str, Any]] = []
    cursor = ""
    seen: set[str] = set()
    while cursor in by_prev:
        ev = by_prev[cursor][0]
        hmac = str(ev["hmac"])
        if hmac in seen:
            raise RecipeRegistryError(
                f"cyclic definition lineage for {label}: receipt {hmac[:16]} is reachable twice",
            )
        seen.add(hmac)
        ordered.append(ev)
        cursor = hmac

    if len(ordered) != len(events):
        superseded = {str(e["hmac"]) for evs in forked.values() for e in evs} - set(seen)
        unreachable = sorted(
            str(e["hmac"])[:16] for e in events if str(e["hmac"]) not in seen and str(e["hmac"]) not in superseded
        )
        if unreachable:
            raise RecipeRegistryError(
                f"unreachable definition-lineage receipt(s) for {label}: {', '.join(unreachable)}; "
                "the receipt chain does not link back to the first registration",
            )
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
