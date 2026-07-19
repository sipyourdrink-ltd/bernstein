"""Schedule supervisor - wakes at the configured cadence and fires schedules.

The supervisor:

1. Iterates the ScheduleStore on each tick.
2. Computes the next fire instant for each schedule using the in-tree
   cron parser.
3. When ``now`` is at or past the next fire instant, it builds a
   :class:`bernstein.core.orchestration.schedule_projection.ProjectionResult`
   from ``(schedule_id, fire_time, last_state)`` and dispatches it
   through the existing trigger pipeline.
4. Records each fire in the audit chain with event_type
   ``schedule.fire`` and a payload carrying the projection_hash.
5. Honours the per-schedule misfire policy:

   - ``skip`` (default): one fire per tick; the supervisor advances
     ``last_fire_at`` to the missed instant without enqueuing a task
     for every interim window. A receipt is written so the operator
     can derive the counterfactual by replaying the lineage.
   - ``catch_up``: one fire per missed instant (bounded by a safety
     cap so a long downtime cannot blow the task queue).

Lifecycle: this class is callable from either a long-running
``bernstein schedule run`` worker OR from inside the existing
``bernstein daemon`` supervisor; both surfaces invoke ``tick``.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

from bernstein.core.orchestration.collision import (
    CollisionAction,
    CollisionPolicy,
    RunningFireState,
    decide_collision,
)
from bernstein.core.orchestration.schedule_projection import (
    SCHEDULE_PROJECTION_REV,
    ProjectionResult,
    project_schedule_fire,
)
from bernstein.core.planning.schedule_store import (
    ParsedCron,
    Schedule,
    ScheduleStore,
    parse_cron,
)
from bernstein.core.trigger_sources.schedule import normalize_schedule_fire

logger = logging.getLogger(__name__)

#: Hard cap on catch-up fires emitted in a single tick. Prevents a
#: long outage from blowing the task queue when the operator opted into
#: catch_up. The remaining missed windows fold into a single counterfactual
#: receipt the operator can replay.
DEFAULT_CATCH_UP_LIMIT = 16

#: Default tick cadence in seconds for the standalone worker.
DEFAULT_TICK_INTERVAL_S = 30.0

#: Event-type string written into the audit chain for each fire.
AUDIT_EVENT_TYPE = "schedule.fire"

#: Event-type string written into the audit chain for each evaluated
#: concurrency collision (#2546). Matches
#: :data:`bernstein.core.security.audit_chain.EVENT_SCHEDULE_COLLISION`.
COLLISION_EVENT_TYPE = "schedule.collision_receipt"

#: Schema rev of the collision decision-input digest. Bumping it changes
#: every ``decision_inputs_hash`` and is the single lever for evolving the
#: recorded input contract.
COLLISION_INPUTS_REV = "1"


def collision_inputs_hash(
    *,
    schedule_id: str,
    fire_time: int,
    running_count: int,
    concurrency_cap: int,
    fire_id: str,
    resume_from_checkpoint: str,
) -> str:
    """Return the canonical digest of the inputs a collision decision used.

    Pure function of the arguments, so an auditor holding a collision event
    can recompute the digest offline and prove the recorded decision was
    taken over exactly these inputs. Recorded alongside the decision in the
    chain event, which makes a collision receipt reproducible rather than
    merely present.

    Args:
        schedule_id: The schedule whose fire collided.
        fire_time: Unix epoch of the contended fire window.
        running_count: How many fires of the schedule were in flight.
        concurrency_cap: The cap the count was compared against.
        fire_id: Identifier of the running fire.
        resume_from_checkpoint: Checkpoint reference a supersede handoff
            would resume from, or ``""``.

    Returns:
        Hex SHA-256 of the canonical JSON encoding of the inputs.
    """
    canonical = json.dumps(
        {
            "rev": COLLISION_INPUTS_REV,
            "schedule_id": schedule_id,
            "fire_time": fire_time,
            "running_count": running_count,
            "concurrency_cap": concurrency_cap,
            "fire_id": fire_id,
            "resume_from_checkpoint": resume_from_checkpoint,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


# ---------------------------------------------------------------------------
# Cron iteration math (in-tree, deterministic)
# ---------------------------------------------------------------------------


def _next_fire_after(parsed: ParsedCron, anchor_epoch: int) -> int:
    """Return the next fire epoch strictly greater than ``anchor_epoch``.

    Iterates minute by minute starting from ``anchor_epoch + 60`` rounded
    down to the minute. Bounded scan: we never look more than 2 years
    ahead, which catches the worst case ``"0 0 29 2 *"`` (Feb 29) without
    spinning forever on an unsatisfiable expression.

    The supervisor calls this with an anchor of either ``last_fire_at``
    or ``now`` depending on whether the schedule has fired before. UTC
    only - the host timezone is not part of the deterministic contract;
    keeping cron evaluation in UTC means two operators on different
    timezones still fire on the same instant.
    """
    # Two-year scan cap (in minutes).
    max_minutes = 2 * 366 * 24 * 60

    # Round the anchor down to the minute, then step one minute forward
    # so "strictly greater than anchor" semantics hold even when the
    # anchor itself is already on a minute boundary.
    start_minute = (anchor_epoch // 60 + 1) * 60
    start_dt = datetime.fromtimestamp(start_minute, tz=UTC)

    for offset in range(max_minutes):
        candidate = start_dt + timedelta(minutes=offset)
        if (
            candidate.minute in parsed.minutes
            and candidate.hour in parsed.hours
            and candidate.month in parsed.months
            and _matches_day(parsed, candidate)
        ):
            return int(candidate.timestamp())

    raise RuntimeError(f"No fire instant found in 2 years for cron expression {parsed.raw!r}")


def _last_processed_window(schedule: Schedule) -> float:
    """Newest window already decided about: a real fire or a canceled one.

    Scheduling reads this; operator-facing surfaces read ``last_fire_at``,
    which only ever records a window that actually dispatched.
    """
    from bernstein.core.planning.schedule_store import FIRE_CURSOR_KEY

    raw = (schedule.extra or {}).get(FIRE_CURSOR_KEY, 0.0)
    try:
        cursor = float(raw)
    except (TypeError, ValueError):
        cursor = 0.0
    return max(float(schedule.last_fire_at), cursor)


def _matches_day(parsed: ParsedCron, dt: datetime) -> bool:
    """POSIX cron day matching: if either ``day`` or ``weekday`` is
    restricted (not a full range), the match is the union of the two.

    Full range = the field expanded to the entire allowed set, which is
    how an operator writes ``*``. Standard 5-field cron semantics.
    """
    full_days = set(range(1, 32))
    full_weekdays = set(range(0, 7))

    days_restricted = set(parsed.days) != full_days
    weekdays_restricted = set(parsed.weekdays) != full_weekdays

    weekday_py_to_cron = (dt.weekday() + 1) % 7  # Monday=1 -> 1; Sunday=6 -> 0

    if days_restricted and weekdays_restricted:
        return dt.day in parsed.days or weekday_py_to_cron in parsed.weekdays
    if days_restricted:
        return dt.day in parsed.days
    if weekdays_restricted:
        return weekday_py_to_cron in parsed.weekdays
    return True


# ---------------------------------------------------------------------------
# Fire receipt
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FireReceipt:
    """Outcome of one fire attempt, persisted under runtime/schedules/receipts/.

    Receipts give the operator a replayable record of every fire and
    every skipped window (when ``misfire_policy == skip``). The
    ``schedule audit`` verb walks these alongside the HMAC chain.

    ``goal`` and ``scenario_id`` are persisted alongside the hash so the
    audit verb can re-derive the projection from the receipt alone
    (#1838) without depending on the live ScheduleStore, which may have
    been edited or removed since the fire. They default to empty for
    backward-compatibility with receipts written before this field
    existed; such legacy receipts re-derive against an empty goal and are
    reported as not-self-contained rather than silently mis-verified.
    """

    schedule_id: str
    fire_time: int
    projection_hash: str
    rev: str
    prev_chain_digest: str
    chain_digest: str
    misfire_policy: str
    dispatched: bool
    skipped_windows: tuple[int, ...] = field(default_factory=tuple)
    counterfactual: bool = False
    goal: str = ""
    scenario_id: str = ""
    # #2545 -- input-contract fields. ``params_hash`` is the validated-param
    # content hash folded into the projection; ``refused`` marks a fire the
    # supervisor refused before any dispatch because its params failed their
    # declared contract, with the refusal receipt's identity recorded here.
    params_hash: str = ""
    refused: bool = False
    refusal_json_path: str = ""
    refusal_receipt_hash: str = ""


# ---------------------------------------------------------------------------
# AuditChainWriter adapter
# ---------------------------------------------------------------------------


class _AuditChainAdapter:
    """Thin adapter so the supervisor can talk to an ``AuditLog`` OR a stub.

    Tests inject an in-memory chain; production wires the existing
    ``bernstein.core.security.audit.AuditLog`` whose HMAC chain primitives
    we re-use (do NOT invent a parallel chain - see AC and ticket Notes).
    """

    def __init__(self, writer: Any) -> None:
        self._writer = writer

    @property
    def chain_tail(self) -> str:
        tail_attr = getattr(self._writer, "_prev_hmac", None)
        if isinstance(tail_attr, str):
            return tail_attr
        return ""

    def append(
        self,
        event_type: str,
        actor: str,
        resource_type: str,
        resource_id: str,
        details: dict[str, Any],
    ) -> str:
        """Append a chain entry and return the new chain digest."""
        event = self._writer.log(event_type, actor, resource_type, resource_id, details)
        # ``AuditLog.log`` returns an AuditEvent with an ``hmac`` field.
        return getattr(event, "hmac", "")


# ---------------------------------------------------------------------------
# ScheduleSupervisor
# ---------------------------------------------------------------------------


@dataclass
class SupervisorStatus:
    """Snapshot of the supervisor for ``bernstein doctor``.

    Attributes:
        alive: True if the supervisor has ticked at least once in the
            doctor-defined liveness window.
        last_tick_at: Epoch of the last tick (0 if never).
        last_fire_at: Epoch of the last successful fire across all
            schedules (0 if no schedule has ever fired).
        next_fire_at: Epoch of the next due fire across all schedules
            (0 if no schedule is currently registered).
        next_fire_schedule_id: ID of the schedule the next fire belongs to.
        schedules_total: Count of registered schedules.
    """

    alive: bool
    last_tick_at: float
    last_fire_at: float
    next_fire_at: float
    next_fire_schedule_id: str
    schedules_total: int


class ScheduleSupervisor:
    """Polls the ScheduleStore and fires due schedules.

    Args:
        store: ScheduleStore instance.
        dispatch: Callable invoked with each ready TriggerEvent. The
            production wiring forwards into TriggerManager.evaluate; tests
            inject a list-append spy.
        audit_writer: Object exposing ``log(event_type, actor,
            resource_type, resource_id, details) -> AuditEvent``. The
            production wiring passes ``AuditLog``; tests pass a stub.
        catch_up_limit: Hard cap on catch-up fires per tick.
    """

    def __init__(
        self,
        store: ScheduleStore,
        dispatch: Callable[[Any], None],
        audit_writer: Any,
        *,
        catch_up_limit: int = DEFAULT_CATCH_UP_LIMIT,
        record_fire_projection: bool = True,
        refusal_chain: Any | None = None,
        install_identity: tuple[str, str] | None = None,
        running_probe: Callable[[Schedule], RunningFireState] | None = None,
        collision_policy: CollisionPolicy | str = CollisionPolicy.CANCEL_NEW,
        concurrency_cap: int = 1,
        sla_monitor: Any | None = None,
    ) -> None:
        self._store = store
        self._dispatch = dispatch
        self._chain = _AuditChainAdapter(audit_writer) if audit_writer is not None else None
        self._catch_up_limit = max(1, catch_up_limit)
        self._record_fire_projection_enabled = record_fire_projection
        # #2546 collision guard. Without a running probe the supervisor keeps
        # its historical behaviour (no liveness check); with one it evaluates
        # a deterministic collision decision before every dispatch and emits a
        # collision receipt for every overlap it considers, so an overrun is a
        # recorded decision rather than a silent double-write.
        self._running_probe = running_probe
        self._collision_policy = CollisionPolicy(collision_policy)
        self._concurrency_cap = max(1, concurrency_cap)
        self._receipts_dir = store.directory.parent / "schedule_receipts"
        self._receipts_dir.mkdir(parents=True, exist_ok=True)
        self._last_tick_at = 0.0
        self._last_fire_at = 0.0
        # #2545 -- input contract. The refusal chain and install identity are
        # resolved lazily from the store's ``.sdd`` directory when a
        # params-declaring schedule first fires, so params-less schedules pay
        # nothing and existing wiring keeps working unchanged. Tests inject
        # both to keep the boundary hermetic.
        self._refusal_chain = refusal_chain
        self._install_identity = install_identity
        # #2549 per-goal SLA contracts. When a monitor is injected the tick
        # evaluates registered contracts against chain evidence after firing
        # schedules. Evaluation is read-only and never dispatches a task; a
        # breach's only side effects are a signed receipt, an ``sla.violation``
        # chain event, and a normalised trigger event. A params-less supervisor
        # (no monitor) is byte-identical to the pre-#2549 behaviour.
        self._sla_monitor = sla_monitor

    # -- Public API ---------------------------------------------------------

    def tick(self, *, now: float | None = None) -> list[FireReceipt]:
        """Run one supervisor tick.

        Returns the list of receipts emitted on this tick (mostly empty
        when no schedule is due). The list is also persisted to disk for
        ``bernstein schedule audit``.

        When an SLA monitor is wired, registered per-goal contracts are
        evaluated against chain evidence after schedules are processed. That
        evaluation is read-only and never dispatches a task, so the fire path
        above is unchanged.
        """
        now_epoch = int(now if now is not None else time.time())
        self._last_tick_at = float(now_epoch)

        receipts: list[FireReceipt] = []
        for schedule in self._store.list():
            try:
                receipts.extend(self._tick_one(schedule, now_epoch))
            except Exception:  # pragma: no cover - defensive
                logger.exception("Supervisor tick failed for schedule %s", schedule.id)
        self._evaluate_sla_contracts(now_epoch)
        return receipts

    def _evaluate_sla_contracts(self, now_epoch: int) -> None:
        """Evaluate registered SLA contracts (read-only; never dispatches).

        A failure here is logged and swallowed so a bad contract can never wedge
        a supervisor tick or interfere with the schedule fire path.
        """
        if self._sla_monitor is None:
            return
        try:
            self._sla_monitor.evaluate(now_epoch)
        except Exception:  # pragma: no cover - defensive
            logger.exception("SLA contract evaluation failed on tick @ %s", now_epoch)

    def status(self, *, liveness_window_s: float = 120.0) -> SupervisorStatus:
        """Produce a doctor-ready snapshot.

        The ``alive`` flag is True iff the supervisor has ticked within
        ``liveness_window_s`` seconds. Operators tune the window through
        the doctor surface; we keep a generous default because a quiet
        schedule catalog still expects the supervisor to ping the store.
        """
        now = time.time()
        alive = (now - self._last_tick_at) <= liveness_window_s if self._last_tick_at else False
        schedules = self._store.list()
        last_fire = self._last_fire_at
        next_fire = 0.0
        next_id = ""
        for schedule in schedules:
            if schedule.last_fire_at > last_fire:
                last_fire = schedule.last_fire_at
            try:
                upcoming = self.next_fire_for(schedule, anchor_epoch=int(now))
            except Exception:  # pragma: no cover - defensive
                continue
            if math.isclose(next_fire, 0.0, abs_tol=1e-12) or upcoming < next_fire:
                next_fire = float(upcoming)
                next_id = schedule.id
        return SupervisorStatus(
            alive=alive,
            last_tick_at=self._last_tick_at,
            last_fire_at=last_fire,
            next_fire_at=next_fire,
            next_fire_schedule_id=next_id,
            schedules_total=len(schedules),
        )

    def next_fire_for(self, schedule: Schedule, *, anchor_epoch: int) -> int:
        """Return the next fire instant for ``schedule`` after ``anchor_epoch``.

        Splits cron parsing from iteration so the supervisor can cache
        parsed cron expressions in future revs without changing the
        external API.
        """
        parsed = parse_cron(schedule.cron)
        anchor = max(anchor_epoch, int(_last_processed_window(schedule)))
        return _next_fire_after(parsed, anchor)

    # -- Internals ----------------------------------------------------------

    def _tick_one(self, schedule: Schedule, now_epoch: int) -> list[FireReceipt]:
        """Tick a single schedule. May emit 0..N receipts."""
        parsed = parse_cron(schedule.cron)
        processed = _last_processed_window(schedule)
        anchor = int(processed) if processed else now_epoch - 60
        receipts: list[FireReceipt] = []
        skipped_windows: list[int] = []

        # Step forward through missed windows up to ``now``.
        current_anchor = anchor
        fires_dispatched = 0
        while True:
            try:
                next_fire = _next_fire_after(parsed, current_anchor)
            except RuntimeError:
                break
            if next_fire > now_epoch:
                break
            if schedule.misfire_policy == "catch_up":
                if fires_dispatched >= self._catch_up_limit:
                    # Fold remaining windows into a counterfactual receipt
                    # so the operator can replay them out-of-band.
                    skipped_windows.append(next_fire)
                    current_anchor = next_fire
                    continue
                receipts.append(self._dispatch_or_collide(schedule, next_fire))
                fires_dispatched += 1
            else:  # skip policy
                # Only dispatch the most recent missed instant; older
                # windows are folded into the skipped_windows receipt.
                if next_fire <= now_epoch:
                    # Peek one further: if there is another miss after
                    # this one but still <= now, this current one is
                    # superseded.
                    try:
                        peek = _next_fire_after(parsed, next_fire)
                    except RuntimeError:
                        peek = None
                    if peek is not None and peek <= now_epoch:
                        skipped_windows.append(next_fire)
                        current_anchor = next_fire
                        continue
                receipts.append(self._dispatch_or_collide(schedule, next_fire))
                fires_dispatched += 1
            current_anchor = next_fire

        if skipped_windows and schedule.misfire_policy in {"skip", "catch_up"}:
            # Emit one counterfactual receipt summarising skipped windows.
            receipts.append(
                self._record_counterfactual(schedule, skipped_windows, now_epoch),
            )

        return receipts

    @staticmethod
    def _resolve_response_profile(schedule: Schedule) -> tuple[str, str]:
        """Resolve the schedule's declared response-style profile, if any.

        A schedule can declare
        ``extra: {response_profile: <verbose|balanced|terse>}``; the profile
        and the SHA-256 of its rendered addendum are then folded into the
        projected task identity. Schedules without the key (every pre-change
        schedule) return ``("", "")`` so their projection stays byte-identical
        to prior revs. Unknown style names are logged and ignored rather than
        blocking the fire - config-level typos are rejected earlier by the
        seed parser.

        The addendum is rendered from the bundled templates (no workdir
        context exists at supervisor level); the spawn-time ledger entry
        records the hash of the addendum actually rendered in the run's
        workdir, which may differ when the operator overrides templates.
        """
        raw = schedule.extra.get("response_profile")
        if not isinstance(raw, str) or not raw:
            return "", ""
        from bernstein.core.agents.response_style import (
            RESPONSE_STYLES,
            ResponseStyleTemplateError,
            addendum_sha256,
            render_style_addendum,
        )

        if raw not in RESPONSE_STYLES:
            from bernstein.core.security.sanitize import sanitize_log

            logger.warning(
                "Schedule %s declares unknown response_profile %r; ignoring",
                schedule.id,
                sanitize_log(raw),
            )
            return "", ""
        try:
            profile_sha = addendum_sha256(render_style_addendum(raw))
        except ResponseStyleTemplateError as exc:
            logger.warning(
                "Schedule %s response_profile %r cannot be rendered (%s); ignoring",
                schedule.id,
                raw,
                exc,
            )
            return "", ""
        return raw, profile_sha

    def _input_contract_deps(self) -> tuple[Any, str, str] | None:
        """Lazily resolve ``(refusal_chain, private_pem, public_pem)`` or None.

        Built from the store's ``.sdd`` directory on first use so a
        params-declaring schedule seals its refusal into the canonical audit
        chain signed with the install identity. Returns None only when neither
        an injected dependency nor an on-disk ``.sdd`` directory is available,
        in which case a params fire is still refused (fail-closed) but without a
        chain-anchored receipt.
        """
        chain = self._refusal_chain
        identity = self._install_identity
        if chain is not None and identity is not None:
            return chain, identity[0], identity[1]
        sdd_dir = getattr(self._store, "sdd_dir", None)
        if sdd_dir is None:
            return None
        try:
            from bernstein.core.lineage.identity import load_or_create_signing_identity
            from bernstein.core.security.audit import load_or_create_audit_key
            from bernstein.core.security.audit_chain import AuditChainStore

            if chain is None:
                chain = AuditChainStore(sdd_dir / "audit", key=load_or_create_audit_key())
                self._refusal_chain = chain
            if identity is None:
                identity = load_or_create_signing_identity(
                    sdd_dir / "identity",
                    private_name="input_refusal.pem",
                    public_name="input_refusal.pub",
                )
                self._install_identity = identity
        except Exception:  # pragma: no cover - defensive
            logger.exception("Could not resolve input-contract deps for %s", self._store)
            return None
        return chain, identity[0], identity[1]

    def _validate_fire_params(self, schedule: Schedule, fire_epoch: int) -> tuple[str, FireReceipt | None]:
        """Validate a schedule's params at fire time (#2545).

        Returns ``(params_hash, refusal_receipt_or_None)``. A params-less
        schedule returns ``("", None)`` and stays byte-identical to a pre-#2545
        fire. A params fire whose values satisfy the declared contract returns
        the validated content hash to fold into the projection. A params fire
        that fails its contract returns a refused :class:`FireReceipt` and NO
        params hash; the caller must not dispatch, so the malformed fire costs
        zero adapter spawns and zero tokens.
        """
        if not schedule.params_schema and not schedule.params:
            return "", None

        from bernstein.core.tasks.param_contract import ParamContract, ParamContractViolation

        contract = ParamContract.from_schema(schedule.params_schema)
        try:
            validated = contract.validate_and_coerce(schedule.params)
        except ParamContractViolation as violation:
            receipt = self._refuse_fire(schedule, fire_epoch, violation)
            return "", receipt
        return contract.params_hash(validated), None

    def _refuse_fire(self, schedule: Schedule, fire_epoch: int, violation: Any) -> FireReceipt:
        """Seal a signed refusal receipt for a malformed fire and persist it."""
        from bernstein.core.security.input_refusal import BOUNDARY_SCHEDULE_FIRE, refuse_input

        receipt_hash = ""
        deps = self._input_contract_deps()
        if deps is not None:
            chain, priv, pub = deps
            sdd_dir = getattr(self._store, "sdd_dir", None)
            try:
                receipt = refuse_input(
                    chain=chain,
                    sdd_dir=sdd_dir,
                    boundary=BOUNDARY_SCHEDULE_FIRE,
                    resource_id=schedule.id,
                    json_path=violation.json_path,
                    schema_hash=violation.schema_hash,
                    value_digest=violation.value_digest,
                    reason_code=violation.reason_code,
                    message=str(violation),
                    private_key_pem=priv,
                    public_key_pem=pub,
                    refused_at=fire_epoch,
                )
                receipt_hash = receipt.receipt_hash()
            except Exception:  # pragma: no cover - defensive
                logger.exception("Could not seal refusal receipt for schedule %s", schedule.id)
        else:
            logger.warning(
                "Schedule %s params failed validation but no audit chain is available to seal a receipt", schedule.id
            )

        logger.warning("Refused malformed fire for schedule %s: %s", schedule.id, violation)
        refused = FireReceipt(
            schedule_id=schedule.id,
            fire_time=fire_epoch,
            projection_hash="",
            rev=SCHEDULE_PROJECTION_REV,
            prev_chain_digest=self._chain.chain_tail if self._chain is not None else "",
            chain_digest="",
            misfire_policy=schedule.misfire_policy,
            dispatched=False,
            goal=schedule.goal,
            scenario_id=schedule.scenario_id,
            refused=True,
            refusal_json_path=str(getattr(violation, "json_path", "")),
            refusal_receipt_hash=receipt_hash,
        )
        self._persist_receipt(refused)
        return refused

    def _dispatch_or_collide(self, schedule: Schedule, fire_epoch: int) -> FireReceipt:
        """Evaluate the collision guard, then dispatch or record a collision.

        With no running probe this is a straight pass-through to
        :meth:`_fire`, preserving the historical dispatch path. With a probe
        that reports a still-running previous fire, the decision is the pure
        :func:`decide_collision` outcome:

        - ``DISPATCH`` / ``SUPERSEDE`` -> fire (supersede resumes from the
          running fire's recorded checkpoint reference, which is threaded
          into the dispatched event so the handoff survives the dispatch);
        - ``ENQUEUE`` -> defer the window; it is re-evaluated next tick;
        - ``CANCEL`` -> drop the window for good, advancing ``last_fire_at``
          so a later idle tick cannot resurrect what was already canceled.

        A collision receipt is emitted for every overlap considered, so a
        double-fire is a recorded, offline-verifiable decision. The receipt
        carries the chain digest of the collision event it rode on and its
        real predecessor, so it is anchored rather than free-floating.
        """
        if self._running_probe is None:
            return self._fire(schedule, fire_epoch, counterfactual=False)
        running = self._running_probe(schedule)
        decision = decide_collision(
            policy=self._collision_policy,
            running=running,
            concurrency_cap=self._concurrency_cap,
        )
        if not (running.running and running.running_count >= self._concurrency_cap):
            # No live collision: dispatch normally (no receipt needed - the
            # fire's own audit entry already records it).
            return self._fire(schedule, fire_epoch, counterfactual=False)

        prev_chain = self._chain.chain_tail if self._chain is not None else ""
        chain_digest = self._append_collision(schedule, fire_epoch, decision, running)
        if decision.dispatch:
            # SUPERSEDE_WITH_HANDOFF: the stale run is checkpointed elsewhere;
            # the new fire proceeds, resuming from the recorded checkpoint.
            return self._fire(
                schedule,
                fire_epoch,
                counterfactual=False,
                resume_from_checkpoint=decision.resume_from_checkpoint,
                warm_resume=decision.warm_resume,
            )
        # ENQUEUE / CANCEL_NEW: record the decision, dispatch nothing.
        receipt = FireReceipt(
            schedule_id=schedule.id,
            fire_time=fire_epoch,
            projection_hash="",
            rev="",
            prev_chain_digest=prev_chain,
            chain_digest=chain_digest,
            misfire_policy=schedule.misfire_policy,
            dispatched=False,
            skipped_windows=(),
            counterfactual=False,
            goal=schedule.goal,
            scenario_id=schedule.scenario_id,
        )
        self._persist_receipt(receipt)
        if decision.action is CollisionAction.CANCEL:
            # CANCEL_NEW drops the window permanently. Persisting it as
            # processed is what makes that permanent: without it the window
            # is still "due" and the next idle tick dispatches the very fire
            # the policy canceled. ENQUEUE deliberately does not advance, so
            # its deferral is retried while the previous fire drains.
            #
            # The *cursor* advances, not ``last_fire_at``: nothing dispatched
            # and no fire entry exists, so reporting it as the last successful
            # fire would be the same class of lie this change set exists to
            # remove - only on the doctor / status surface instead.
            self._store.advance_fire_cursor(schedule.id, float(fire_epoch))
        return receipt

    def _append_collision(
        self,
        schedule: Schedule,
        fire_epoch: int,
        decision: Any,
        running: RunningFireState,
    ) -> str:
        """Append a ``schedule.collision_receipt`` entry to the audit chain.

        Mirrors :meth:`_append_audit` so the collision decision rides the
        same HMAC chain as the fires it guards. Returns the new chain digest
        (or the stable receipt hash when no chain is wired).

        The entry also carries ``decision_inputs_hash``: a canonical digest
        of the running count, the cap, the running fire id, and the
        checkpoint reference. An auditor can recompute it with
        :func:`collision_inputs_hash` and prove the recorded decision was
        taken over exactly those inputs.
        """
        if self._chain is None:
            return decision.receipt_hash
        return self._chain.append(
            event_type=COLLISION_EVENT_TYPE,
            actor="schedule_supervisor",
            resource_type="schedule_collision",
            resource_id=schedule.id,
            details={
                "schedule_id": schedule.id,
                "fire_time": fire_epoch,
                "policy": str(decision.policy),
                "action": str(decision.action),
                "receipt_hash": decision.receipt_hash,
                "resume_from_checkpoint": decision.resume_from_checkpoint,
                "warm_resume": decision.warm_resume,
                "running_count": running.running_count,
                "concurrency_cap": self._concurrency_cap,
                "running_fire_id": running.fire_id,
                "decision_inputs_hash": collision_inputs_hash(
                    schedule_id=schedule.id,
                    fire_time=fire_epoch,
                    running_count=running.running_count,
                    concurrency_cap=self._concurrency_cap,
                    fire_id=running.fire_id,
                    resume_from_checkpoint=decision.resume_from_checkpoint,
                ),
            },
        )

    def _fire(
        self,
        schedule: Schedule,
        fire_epoch: int,
        *,
        counterfactual: bool,
        resume_from_checkpoint: str | None = None,
        warm_resume: bool = False,
    ) -> FireReceipt:
        """Build the projection, dispatch the trigger event, and chain it.

        A parameterized schedule is validated first (#2545): a malformed fire is
        refused with a signed receipt and returns before any projection or
        dispatch, so it costs zero adapter spawns. A valid fire folds the
        validated params hash into the deterministic projection.

        ``resume_from_checkpoint`` / ``warm_resume`` carry a supersede
        handoff into the dispatched event; ``None`` means the fire is not a
        handoff at all and the keys stay off the event, keeping an ordinary
        fire byte-identical to prior revs. Dropping a real handoff would
        hand the new fire a cold start while the collision receipt still
        claimed a warm resume, so the two surfaces must agree.
        """
        params_hash, refusal = self._validate_fire_params(schedule, fire_epoch)
        if refusal is not None:
            return refusal

        response_profile, profile_sha = self._resolve_response_profile(schedule)
        projection = project_schedule_fire(
            schedule_id=schedule.id,
            fire_time=fire_epoch,
            last_state=None,
            goal=schedule.goal,
            scenario_id=schedule.scenario_id,
            response_profile=response_profile,
            profile_content_sha256=profile_sha,
            params_hash=params_hash,
        )

        prev_chain = self._chain.chain_tail if self._chain is not None else ""
        chain_digest = self._append_audit(schedule, projection, prev_chain, counterfactual)

        if not counterfactual:
            # #2302: seal the fire projection (with the schedule's recurrence
            # folded in) into the journal + lineage spine for ``schedule
            # verify``. This is a distinct surface from the #1798 receipt
            # ``projection_hash`` above (which stays recurrence-free so
            # existing receipt chains re-derive byte-identically).
            recurrence = f"cron:{schedule.cron}" if schedule.cron else ""
            self._record_fire_projection(schedule, fire_epoch, recurrence)
            extra: dict[str, Any] = {"chain_digest": chain_digest}
            if resume_from_checkpoint is not None:
                # A supersede handoff. ``""`` is a real answer here (the
                # checkpoint failed verification, so the resume is cold);
                # recording it keeps the dispatched event honest instead of
                # letting the consumer guess.
                extra["resume_from_checkpoint"] = resume_from_checkpoint
                extra["warm_resume"] = warm_resume
            event = normalize_schedule_fire(
                schedule_id=schedule.id,
                fire_time=float(fire_epoch),
                goal=schedule.goal,
                scenario_id=schedule.scenario_id,
                projection_hash=projection.projection_hash,
                misfire_policy=schedule.misfire_policy,
                extra=extra,
            )
            try:
                self._dispatch(event)
            except Exception:  # pragma: no cover - defensive
                logger.exception("Dispatch failed for schedule %s @ %s", schedule.id, fire_epoch)
            self._store.update_last_fire(schedule.id, float(fire_epoch))
            self._last_fire_at = max(self._last_fire_at, float(fire_epoch))

        receipt = FireReceipt(
            schedule_id=schedule.id,
            fire_time=fire_epoch,
            projection_hash=projection.projection_hash,
            rev=projection.rev,
            prev_chain_digest=prev_chain,
            chain_digest=chain_digest,
            misfire_policy=schedule.misfire_policy,
            dispatched=not counterfactual,
            skipped_windows=(),
            counterfactual=counterfactual,
            goal=schedule.goal,
            scenario_id=schedule.scenario_id,
            params_hash=params_hash,
        )
        self._persist_receipt(receipt)
        return receipt

    def _record_fire_projection(self, schedule: Schedule, fire_epoch: int, recurrence: str) -> None:
        """Seal the fire projection into the journal and lineage spine (#2302).

        Each dispatched fire records ``{schedule_id, fire_time,
        last_state_hash, graph_hash}`` into the run event journal and seals
        the canonical graph bytes into the lineage spine, so ``schedule
        verify`` can replay the fire and prove the graph hash reproduces.
        The recording is provenance, not the dispatch itself: a failure
        here is logged and swallowed so it never wedges a supervisor tick.
        """
        if not self._record_fire_projection_enabled:
            return
        try:
            from bernstein.core.orchestration.schedule_fire_record import record_fire

            record_fire(
                sdd_dir=self._store.sdd_dir,
                schedule_id=schedule.id,
                fire_time=fire_epoch,
                last_state=None,
                goal=schedule.goal,
                scenario_id=schedule.scenario_id,
                recurrence=recurrence,
            )
        except Exception:  # pragma: no cover - defensive: provenance is best-effort here
            logger.exception("Fire-projection recording failed for schedule %s @ %s", schedule.id, fire_epoch)

    def _record_counterfactual(
        self,
        schedule: Schedule,
        skipped: list[int],
        now_epoch: int,
    ) -> FireReceipt:
        """Emit a counterfactual receipt summarising skipped windows.

        Pure record, no dispatch and no audit-chain entry. Lets the
        operator replay the counterfactual by feeding the skipped epochs
        back into the projection.
        """
        if not skipped:
            return FireReceipt(
                schedule_id=schedule.id,
                fire_time=now_epoch,
                projection_hash="",
                rev="",
                prev_chain_digest="",
                chain_digest="",
                misfire_policy=schedule.misfire_policy,
                dispatched=False,
                skipped_windows=(),
                counterfactual=True,
            )
        receipt = FireReceipt(
            schedule_id=schedule.id,
            fire_time=skipped[-1],
            projection_hash="",
            rev="",
            prev_chain_digest="",
            chain_digest="",
            misfire_policy=schedule.misfire_policy,
            dispatched=False,
            skipped_windows=tuple(skipped),
            counterfactual=True,
        )
        self._persist_receipt(receipt)
        return receipt

    def _append_audit(
        self,
        schedule: Schedule,
        projection: ProjectionResult,
        prev_chain: str,
        counterfactual: bool,
    ) -> str:
        """Append a ``schedule.fire`` entry to the audit chain.

        The payload carries ``(schedule_id, fire_time, projection_hash,
        prev_chain_digest)`` as called out in the AC, plus the projection
        inputs ``goal`` and ``scenario_id`` so the audit verb can
        re-derive the projection from the chain entry alone and prove the
        recorded hash is the genuine projection (#1838). Counterfactual
        receipts skip the chain because they represent fires that did
        NOT happen; mixing them into the chain would defeat the
        byte-identical sequence guarantee.
        """
        if counterfactual or self._chain is None:
            return prev_chain
        details = {
            "schedule_id": schedule.id,
            "fire_time": projection.fire_time,
            "projection_hash": projection.projection_hash,
            "rev": projection.rev,
            "misfire_policy": schedule.misfire_policy,
            "prev_chain_digest": prev_chain,
            "goal": schedule.goal,
            "scenario_id": schedule.scenario_id,
        }
        return self._chain.append(
            event_type=AUDIT_EVENT_TYPE,
            actor="schedule_supervisor",
            resource_type="schedule",
            resource_id=schedule.id,
            details=details,
        )

    def _persist_receipt(self, receipt: FireReceipt) -> None:
        """Write a receipt to disk for ``schedule audit`` to walk later.

        Receipt filenames bake the schedule id and fire instant so two
        operators inspecting their receipts side by side can tell at a
        glance whether their byte-identical sequence held.
        """
        suffix = "counterfactual" if receipt.counterfactual else "fire"
        filename = f"{receipt.schedule_id}-{receipt.fire_time}-{suffix}.json"
        path = self._receipts_dir / filename
        payload = asdict(receipt)
        # Tuples → lists for json. asdict already does this.
        path.write_text(json.dumps(payload, sort_keys=True, indent=2))


# ---------------------------------------------------------------------------
# Receipt loader for ``schedule audit``
# ---------------------------------------------------------------------------


def load_receipts(sdd_dir: Path) -> list[FireReceipt]:
    """Load all persisted receipts in chronological order.

    Used by ``bernstein schedule audit`` to walk the recorded fire
    sequence and verify it is byte-identical to the operator
    expectation.
    """
    receipts_dir = sdd_dir / "runtime" / "schedule_receipts"
    if not receipts_dir.exists():
        return []
    out: list[FireReceipt] = []
    for path in sorted(receipts_dir.glob("*.json")):
        try:
            data = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("Could not load receipt %s: %s", path, exc)
            continue
        try:
            out.append(
                FireReceipt(
                    schedule_id=str(data["schedule_id"]),
                    fire_time=int(data["fire_time"]),
                    projection_hash=str(data.get("projection_hash", "")),
                    rev=str(data.get("rev", "")),
                    prev_chain_digest=str(data.get("prev_chain_digest", "")),
                    chain_digest=str(data.get("chain_digest", "")),
                    misfire_policy=str(data.get("misfire_policy", "skip")),
                    dispatched=bool(data.get("dispatched", False)),
                    skipped_windows=tuple(int(x) for x in data.get("skipped_windows", [])),
                    counterfactual=bool(data.get("counterfactual", False)),
                    goal=str(data.get("goal", "")),
                    scenario_id=str(data.get("scenario_id", "")),
                ),
            )
        except (KeyError, TypeError, ValueError) as exc:
            logger.warning("Malformed receipt %s: %s", path, exc)
    out.sort(key=lambda r: (r.fire_time, r.schedule_id))
    return out


# ---------------------------------------------------------------------------
# Audit verification for ``schedule audit`` (#1838)
# ---------------------------------------------------------------------------


def _read_schedule_fire_entries(sdd_dir: Path) -> dict[tuple[str, int], dict[str, Any]]:
    """Read recorded ``schedule.fire`` chain entries keyed by (id, fire_time).

    Returns a mapping from ``(schedule_id, fire_time)`` to the recorded
    chain payload, enriched with the entry's own ``hmac`` under the
    ``__hmac__`` key so the caller can cross-check the receipt's
    ``chain_digest`` against the entry's HMAC.

    This reads the on-disk JSONL payload only; HMAC-chain tamper-evidence
    is the job of :meth:`AuditLog.verify`. The cross-check here proves a
    receipt agrees with the chain it claims to be anchored in - a receipt
    edited independently of the chain (or a chain entry edited
    independently of the receipt) is caught even when each file is
    internally well-formed.
    """
    audit_dir = sdd_dir / "audit"
    if not audit_dir.exists():
        return {}
    entries: dict[tuple[str, int], dict[str, Any]] = {}
    for path in sorted(audit_dir.glob("*.jsonl")):
        try:
            lines = path.read_text().splitlines()
        except OSError as exc:  # pragma: no cover - defensive
            logger.warning("Could not read audit log %s: %s", path, exc)
            continue
        for line in lines:
            if not line.strip():
                continue
            try:
                parsed: Any = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(parsed, dict):
                continue
            entry = cast("dict[str, Any]", parsed)
            if entry.get("event_type") != AUDIT_EVENT_TYPE:
                continue
            details_raw = entry.get("details")
            if not isinstance(details_raw, dict):
                continue
            details = cast("dict[str, Any]", details_raw)
            try:
                key = (str(details["schedule_id"]), int(details["fire_time"]))
            except (KeyError, TypeError, ValueError):
                continue
            enriched: dict[str, Any] = details.copy()
            enriched["__hmac__"] = str(entry.get("hmac", ""))
            # Last writer wins: later daily segments override earlier ones
            # for the same (id, fire_time), which cannot legitimately
            # collide within one chain anyway.
            entries[key] = enriched
    return entries


@dataclass(frozen=True)
class ReceiptVerification:
    """Per-receipt outcome of the ``schedule audit`` verification walk.

    Attributes:
        schedule_id: Schedule the receipt belongs to.
        fire_time: Fire instant the receipt records.
        counterfactual: True for skip/catch-up summary receipts that carry
            empty hashes by design; these are skipped, never flagged.
        rev: Projection rev recorded on the receipt.
        recorded_projection_hash: The hash stored on the receipt.
        recomputed_projection_hash: The hash re-derived from the receipt's
            persisted inputs under its recorded rev (empty when skipped).
        projection_match: recomputed == recorded (only meaningful when the
            rev could be re-derived).
        chain_match: The receipt agrees with the matching chain entry's
            ``projection_hash``, ``prev_chain_digest`` and HMAC.
        rev_skipped: The receipt's rev differs from the current projection
            rev, so it cannot be re-derived with the in-tree algorithm.
        reasons: Human-readable reasons the receipt failed (empty on pass).
    """

    schedule_id: str
    fire_time: int
    counterfactual: bool
    rev: str
    recorded_projection_hash: str
    recomputed_projection_hash: str
    projection_match: bool
    chain_match: bool
    rev_skipped: bool
    reasons: tuple[str, ...] = field(default_factory=tuple)

    @property
    def skipped(self) -> bool:
        """True when this receipt was intentionally not verified."""
        return self.counterfactual or self.rev_skipped

    @property
    def verified(self) -> bool:
        """True when the receipt passed every applicable check."""
        if self.counterfactual:
            return True
        if self.rev_skipped:
            # Honest stance: a foreign-rev receipt is neither verified nor
            # a mismatch; it is simply not re-derivable here.
            return False
        return self.projection_match and self.chain_match and not self.reasons

    @property
    def mismatch(self) -> bool:
        """True when the receipt failed a check it was eligible for."""
        if self.skipped:
            return False
        return not self.verified


@dataclass(frozen=True)
class AuditReport:
    """Aggregate result of verifying every persisted fire receipt."""

    results: tuple[ReceiptVerification, ...]
    failures: tuple[str, ...]

    @property
    def ok(self) -> bool:
        """True when no receipt reported a mismatch or linkage break."""
        return not self.failures

    def to_json(self) -> list[dict[str, Any]]:
        """Return a JSON-safe per-receipt view for ``--json`` output."""
        return [
            {
                "schedule_id": r.schedule_id,
                "fire_time": r.fire_time,
                "counterfactual": r.counterfactual,
                "rev": r.rev,
                "recorded_projection_hash": r.recorded_projection_hash,
                "recomputed_projection_hash": r.recomputed_projection_hash,
                "projection_match": r.projection_match,
                "chain_match": r.chain_match,
                "rev_skipped": r.rev_skipped,
                "skipped": r.skipped,
                "verified": r.verified,
                "mismatch": r.mismatch,
                "reasons": list(r.reasons),
            }
            for r in self.results
        ]


def verify_receipts(sdd_dir: Path) -> AuditReport:
    """Re-derive and cross-check every persisted fire receipt (#1838).

    For each non-counterfactual receipt this:

    1. Re-runs ``project_schedule_fire`` from the receipt's persisted
       inputs (``schedule_id``, ``fire_time``, ``goal``, ``scenario_id``,
       ``last_state=None``) under the receipt's recorded ``rev`` and
       confirms the recomputed ``projection_hash`` equals the recorded
       one. A mismatch names the receipt.
    2. Cross-checks the receipt against the matching ``schedule.fire``
       audit-chain entry: the entry's ``projection_hash`` must equal the
       receipt's, the entry's ``prev_chain_digest`` must equal the
       receipt's, and the entry's HMAC must equal the receipt's
       ``chain_digest``. A receipt edited independently of the chain (or
       vice versa) is caught.
    3. Verifies the receipt-to-receipt ``prev_chain_digest -> chain_digest``
       linkage forms an unbroken sequence across consecutive dispatched
       receipts.

    Counterfactual receipts carry empty hashes by design and are skipped.
    Receipts recorded under a different projection rev than the current
    in-tree algorithm cannot be re-derived here and are reported as
    rev-skipped rather than false-flagged - the verifier honours
    ``receipt.rev`` rather than the current rev (per the AC).

    ``last_state`` is assumed ``None`` because the only supervisor caller
    fires with ``last_state=None``; the receipt does not (yet) persist a
    folded state. When/if a future rev folds load-bearing state into the
    projection, the receipt must persist enough to reproduce it and this
    function must read it back.

    Returns an :class:`AuditReport`; ``report.ok`` is True only when every
    eligible receipt verified and the chain linkage is unbroken.
    """
    from bernstein.core.orchestration.schedule_projection import (
        SCHEDULE_PROJECTION_REV,
        project_schedule_fire,
    )

    receipts = load_receipts(sdd_dir)
    chain_entries = _read_schedule_fire_entries(sdd_dir)

    results: list[ReceiptVerification] = []
    failures: list[str] = []

    for receipt in receipts:
        name = f"{receipt.schedule_id}@{receipt.fire_time}"
        reasons: list[str] = []

        if receipt.counterfactual:
            results.append(
                ReceiptVerification(
                    schedule_id=receipt.schedule_id,
                    fire_time=receipt.fire_time,
                    counterfactual=True,
                    rev=receipt.rev,
                    recorded_projection_hash=receipt.projection_hash,
                    recomputed_projection_hash="",
                    projection_match=True,
                    chain_match=True,
                    rev_skipped=False,
                ),
            )
            continue

        rev_skipped = receipt.rev != SCHEDULE_PROJECTION_REV
        recomputed = ""
        projection_match = False
        if rev_skipped:
            reasons.append(
                f"receipt rev {receipt.rev!r} != current rev {SCHEDULE_PROJECTION_REV!r}; "
                "cannot re-derive with the in-tree projection algorithm"
            )
        else:
            rebuilt = project_schedule_fire(
                schedule_id=receipt.schedule_id,
                fire_time=receipt.fire_time,
                last_state=None,
                goal=receipt.goal,
                scenario_id=receipt.scenario_id,
            )
            recomputed = rebuilt.projection_hash
            projection_match = recomputed == receipt.projection_hash
            if not projection_match:
                reasons.append(
                    f"projection hash mismatch: recorded {receipt.projection_hash[:16]}… "
                    f"!= recomputed {recomputed[:16]}…"
                )

        # Chain cross-check.
        chain_match = True
        entry = chain_entries.get((receipt.schedule_id, receipt.fire_time))
        if entry is None:
            # A dispatched fire MUST have a chain entry. Its absence is a
            # mismatch unless the chain was never wired (no audit dir).
            if chain_entries or (sdd_dir / "audit").exists():
                chain_match = False
                reasons.append("no matching schedule.fire entry in the audit chain")
        else:
            if entry.get("projection_hash") != receipt.projection_hash:
                chain_match = False
                reasons.append(
                    "chain projection_hash disagrees with receipt: "
                    f"chain {str(entry.get('projection_hash'))[:16]}… "
                    f"!= receipt {receipt.projection_hash[:16]}…"
                )
            if str(entry.get("prev_chain_digest", "")) != receipt.prev_chain_digest:
                chain_match = False
                reasons.append("chain prev_chain_digest disagrees with receipt")
            if str(entry.get("__hmac__", "")) != receipt.chain_digest:
                chain_match = False
                reasons.append("chain entry HMAC disagrees with receipt chain_digest")

        result = ReceiptVerification(
            schedule_id=receipt.schedule_id,
            fire_time=receipt.fire_time,
            counterfactual=False,
            rev=receipt.rev,
            recorded_projection_hash=receipt.projection_hash,
            recomputed_projection_hash=recomputed,
            projection_match=projection_match,
            chain_match=chain_match,
            rev_skipped=rev_skipped,
            reasons=tuple(reasons),
        )
        results.append(result)
        if result.mismatch:
            failures.append(f"{name}: " + "; ".join(reasons))

    # Receipt-to-receipt chain linkage: each dispatched receipt's
    # prev_chain_digest must equal the previous dispatched receipt's
    # chain_digest, forming an unbroken sequence (mirrors the audit
    # chain's prev_hmac linkage).
    dispatched = [r for r in receipts if r.dispatched and not r.counterfactual]
    prev_digest: str | None = None
    for receipt in dispatched:
        if prev_digest is not None and receipt.prev_chain_digest != prev_digest:
            failures.append(
                f"{receipt.schedule_id}@{receipt.fire_time}: chain linkage break - "
                f"prev_chain_digest {receipt.prev_chain_digest[:16]}… "
                f"!= previous chain_digest {prev_digest[:16]}…"
            )
        prev_digest = receipt.chain_digest

    return AuditReport(results=tuple(results), failures=tuple(failures))
