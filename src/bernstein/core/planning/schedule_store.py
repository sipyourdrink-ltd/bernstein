"""Operator-registered recurring schedule store.

Persists operator-registered schedules as flat JSON documents under
``.sdd/runtime/schedules/<id>.json``. Each schedule carries:

- a cron expression (5-field standard form: minute hour dom month dow),
- the goal text (or scenario id) to fire,
- a misfire policy (skip vs catch-up),
- bookkeeping (created_at, last_fire_at).

Issue #1798 - the symmetric in-project surface for recurring goals so
operators do not depend on host-level systemd or cron or an external
cloud scheduler.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)

#: Maximum length of a stored goal text (defensive cap on per-schedule JSON).
_MAX_GOAL_LEN = 4096

#: Maximum length of a stored scenario id.
_MAX_SCENARIO_ID_LEN = 256

#: Schedule id format: 12 hex chars from a sha256 of the canonical schedule body.
_SCHEDULE_ID_PREFIX = "sched_"
_SCHEDULE_ID_HEX_LEN = 12


MisfirePolicy = Literal["skip", "catch_up"]

#: ``Schedule.extra`` key holding the scheduling cursor: the newest fire
#: window the supervisor has already decided about, whether or not it
#: dispatched. Kept out of ``last_fire_at`` so a canceled window never reads
#: as a successful fire on the doctor / status surfaces.
FIRE_CURSOR_KEY = "fire_cursor_at"


@dataclass(frozen=True)
class Schedule:
    """An operator-registered recurring schedule.

    Attributes:
        id: Stable identifier derived from a canonical hash of the cron
            expression plus the goal/scenario body. Equal cron+goal pairs
            land on the same id, which is desirable for idempotent
            ``schedule add`` from configuration.
        cron: Cron expression in standard 5-field form.
        goal: Free-form goal text to dispatch through the trigger
            pipeline. Either ``goal`` or ``scenario_id`` must be set.
        scenario_id: Optional named scenario to invoke instead of (or
            alongside) a free-form goal.
        misfire_policy: ``"skip"`` (default) drops missed fire windows;
            ``"catch_up"`` enqueues one fire per missed window when the
            supervisor wakes.
        created_at: Unix epoch when the schedule was registered.
        last_fire_at: Unix epoch of the last successful fire, or 0 if the
            schedule has not fired since registration.
        params_schema: Optional typed parameter declarations (#2545) - a list
            of :class:`bernstein.core.tasks.param_contract.ParamSpec` schema
            dicts. Empty (the default, and every pre-#2545 schedule) keeps the
            schedule id and projection byte-identical to prior revs.
        params: Operator-supplied raw parameter values validated and coerced
            against ``params_schema`` at fire time. Empty by default.
    """

    id: str
    cron: str
    goal: str = ""
    scenario_id: str = ""
    misfire_policy: MisfirePolicy = "skip"
    created_at: float = 0.0
    last_fire_at: float = 0.0
    extra: dict[str, Any] = field(default_factory=dict[str, Any])
    params_schema: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])
    params: dict[str, Any] = field(default_factory=dict[str, Any])


class CronParseError(ValueError):
    """Raised when a cron expression fails validation."""


# ---------------------------------------------------------------------------
# Minimal deterministic cron parser (5-field standard form)
# ---------------------------------------------------------------------------
#
# Why a small in-tree parser instead of croniter: the AC explicitly bans
# adding a new runtime dependency without justification, and the existing
# trigger_manager only imports croniter inside a ``try/except ImportError``
# (so the codebase already runs without it). The supervisor needs cron
# math that works in the wheelhouse, so we ship a self-contained parser
# limited to the standard 5-field form. The projection function never
# calls cron evaluation - cron evaluation only fires the supervisor; the
# projection itself is pure.

_FIELD_RANGES: tuple[tuple[int, int], ...] = (
    (0, 59),  # minute
    (0, 23),  # hour
    (1, 31),  # day of month
    (1, 12),  # month
    (0, 6),  # day of week (0 = Sunday)
)

_MONTH_NAMES = {
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "may": 5,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "oct": 10,
    "nov": 11,
    "dec": 12,
}

_DOW_NAMES = {
    "sun": 0,
    "mon": 1,
    "tue": 2,
    "wed": 3,
    "thu": 4,
    "fri": 5,
    "sat": 6,
}


def _expand_field(spec: str, lo: int, hi: int, names: dict[str, int] | None = None) -> frozenset[int]:
    """Expand one cron field into the explicit set of matching values.

    Supports ``*``, lists (``a,b``), ranges (``a-b``), and steps
    (``*/n`` or ``a-b/n``). Named months and weekdays resolve via
    ``names`` when supplied.

    Raises:
        CronParseError: On any malformed sub-expression. Surfacing parse
            errors as a typed exception lets the CLI / store wrap them
            with friendly context without losing the cause.
    """
    spec = spec.strip()
    if not spec:
        raise CronParseError("empty cron field")

    result: set[int] = set()
    for part in spec.split(","):
        step = 1
        if "/" in part:
            base, step_str = part.split("/", 1)
            try:
                step = int(step_str)
            except ValueError as exc:
                raise CronParseError(f"invalid step in {part!r}") from exc
            if step <= 0:
                raise CronParseError(f"non-positive step in {part!r}")
        else:
            base = part

        if base in ("*", ""):
            start, end = lo, hi
        elif "-" in base:
            a, b = base.split("-", 1)
            start = _resolve_atom(a, lo, hi, names)
            end = _resolve_atom(b, lo, hi, names)
            if start > end:
                raise CronParseError(f"reversed range in {part!r}")
        else:
            value = _resolve_atom(base, lo, hi, names)
            if step == 1:
                result.add(value)
                continue
            start, end = value, hi

        result.update(range(start, end + 1, step))

    if not result:
        raise CronParseError(f"empty cron field expansion: {spec!r}")
    return frozenset(result)


def _resolve_atom(atom: str, lo: int, hi: int, names: dict[str, int] | None) -> int:
    """Resolve a single cron atom (a number or a named month/weekday)."""
    atom = atom.strip().lower()
    if names is not None and atom in names:
        value = names[atom]
    else:
        try:
            value = int(atom)
        except ValueError as exc:
            raise CronParseError(f"non-numeric cron atom {atom!r}") from exc
    if not lo <= value <= hi:
        raise CronParseError(f"cron value {value} out of range [{lo}, {hi}]")
    return value


@dataclass(frozen=True)
class ParsedCron:
    """A parsed cron expression: each field as the explicit matching set.

    The supervisor uses this to step forward from a UTC datetime to the
    next fire instant without depending on third-party libraries. The
    projection function does NOT use this - cron evaluation is part of
    when the supervisor wakes the projection, not part of the
    deterministic task-graph build.
    """

    minutes: frozenset[int]
    hours: frozenset[int]
    days: frozenset[int]
    months: frozenset[int]
    weekdays: frozenset[int]
    raw: str


def parse_cron(expression: str) -> ParsedCron:
    """Parse a 5-field cron expression into ``ParsedCron``.

    Raises:
        CronParseError: When the expression does not have exactly five
            whitespace-separated fields, or any field is malformed.
    """
    # We accept ``str`` per the type contract; an explicit runtime guard
    # would be redundant under pyright but useful when an untyped caller
    # hands us a None. We tolerate that case by surfacing it as a typed
    # ``AttributeError`` on the next line; downstream code already
    # catches generic parse errors.
    fields = expression.strip().split()
    if len(fields) != 5:
        raise CronParseError(f"expected 5 cron fields (minute hour day month weekday), got {len(fields)}")
    minutes = _expand_field(fields[0], *_FIELD_RANGES[0])
    hours = _expand_field(fields[1], *_FIELD_RANGES[1])
    days = _expand_field(fields[2], *_FIELD_RANGES[2])
    months = _expand_field(fields[3], *_FIELD_RANGES[3], _MONTH_NAMES)
    weekdays = _expand_field(fields[4], *_FIELD_RANGES[4], _DOW_NAMES)
    return ParsedCron(
        minutes=minutes,
        hours=hours,
        days=days,
        months=months,
        weekdays=weekdays,
        raw=expression.strip(),
    )


def validate_cron(expression: str) -> None:
    """Validate a cron expression by attempting to parse it.

    Raises:
        CronParseError: When the expression is malformed.
    """
    parse_cron(expression)


# ---------------------------------------------------------------------------
# Schedule store
# ---------------------------------------------------------------------------


_GOAL_PATTERN = re.compile(r"[\r\n\t]+")


def _canonical_schedule_id(
    cron: str,
    goal: str,
    scenario_id: str,
    params_schema: list[dict[str, Any]] | None = None,
    params: dict[str, Any] | None = None,
) -> str:
    """Derive a stable schedule id from the canonical schedule body.

    Equal bodies land on the same id so reapplying ``schedule add`` from
    configuration is idempotent. The hex length is intentionally short
    (12 chars) so the id stays grep-friendly while keeping collision
    probability low for the operator's catalog.

    The parameter block (#2545) is folded into the id ONLY when non-empty, so a
    schedule without params keeps the exact id it had before params existed
    (AC5); two schedules that differ only in their params get distinct ids.
    """
    body: dict[str, Any] = {"cron": cron.strip(), "goal": goal.strip(), "scenario_id": scenario_id.strip()}
    if params_schema:
        body["params_schema"] = params_schema
    if params:
        body["params"] = params
    canonical = json.dumps(body, sort_keys=True)
    digest = hashlib.sha256(canonical.encode()).hexdigest()
    return f"{_SCHEDULE_ID_PREFIX}{digest[:_SCHEDULE_ID_HEX_LEN]}"


def _sanitize_goal(text: str) -> str:
    """Clamp goal text length and collapse forbidden whitespace.

    Keeping schedule bodies bounded prevents an operator from accidentally
    persisting a multi-megabyte goal that the supervisor would then have
    to load on every tick.
    """
    cleaned = _GOAL_PATTERN.sub(" ", text).strip()
    return cleaned[:_MAX_GOAL_LEN]


class ScheduleStore:
    """File-backed store for operator-registered schedules.

    One JSON file per schedule under ``<sdd_dir>/runtime/schedules/``. The
    store is the source of truth; the supervisor reads from it on each
    tick. ``add`` is idempotent: adding the same ``(cron, goal, scenario_id)``
    triple twice returns the existing schedule unchanged.
    """

    def __init__(self, sdd_dir: Path) -> None:
        self._sdd_dir = sdd_dir
        self._dir = sdd_dir / "runtime" / "schedules"
        self._dir.mkdir(parents=True, exist_ok=True)

    @property
    def directory(self) -> Path:
        return self._dir

    @property
    def sdd_dir(self) -> Path:
        """Return the project ``.sdd`` directory backing this store.

        The fire-record boundary (#2302) writes each fire's journal and
        lineage-spine entries under this root, so it is surfaced alongside
        the schedules directory.
        """
        return self._sdd_dir

    def _path_for(self, schedule_id: str) -> Path:
        return self._dir / f"{schedule_id}.json"

    def add(
        self,
        *,
        cron: str,
        goal: str = "",
        scenario_id: str = "",
        misfire_policy: MisfirePolicy = "skip",
        params_schema: list[dict[str, Any]] | None = None,
        params: dict[str, Any] | None = None,
        now: float | None = None,
    ) -> Schedule:
        """Register a schedule.

        Args:
            cron: Cron expression in 5-field standard form.
            goal: Goal text (mutually exclusive with ``scenario_id`` is
                NOT required - both may be set).
            scenario_id: Named scenario id.
            misfire_policy: ``"skip"`` (default) or ``"catch_up"``.
            params_schema: Optional typed parameter declarations (#2545). When
                provided, the declared ``params`` are validated and coerced at
                registration time so a malformed default cannot be stored.
            params: Optional raw parameter values checked against
                ``params_schema``.
            now: Optional override for the creation timestamp (test
                determinism). Defaults to ``time.time()``.

        Raises:
            CronParseError: When the cron expression is malformed.
            ValueError: When neither goal nor scenario_id is set, when a stored
                value exceeds its length cap, or when the declared params fail
                their own schema.
        """
        validate_cron(cron)
        clean_goal = _sanitize_goal(goal)
        clean_scenario = scenario_id.strip()[:_MAX_SCENARIO_ID_LEN]
        if not clean_goal and not clean_scenario:
            raise ValueError("schedule must have at least one of goal or scenario_id")
        if misfire_policy not in ("skip", "catch_up"):
            raise ValueError(f"unknown misfire policy: {misfire_policy!r}")

        schema_list = list(params_schema or [])
        params_map = dict(params or {})
        if schema_list:
            # Validate the schema *shape* at registration so a malformed
            # declaration is caught early. The parameter *values* are the
            # fire-time gate (#2545): the supervisor validates and coerces them
            # before dispatch and seals a signed refusal receipt on failure, so
            # a stored value that later drifts out of contract (a tightened
            # schema, a mistyped edit) is refused at fire rather than silently
            # dispatched.
            from bernstein.core.tasks.param_contract import ParamContract

            ParamContract.from_schema(schema_list)

        schedule_id = _canonical_schedule_id(cron, clean_goal, clean_scenario, schema_list, params_map)
        existing = self.get(schedule_id)
        if existing is not None:
            return existing

        created_at = float(now) if now is not None else time.time()
        schedule = Schedule(
            id=schedule_id,
            cron=cron.strip(),
            goal=clean_goal,
            scenario_id=clean_scenario,
            misfire_policy=misfire_policy,
            created_at=created_at,
            last_fire_at=0.0,
            params_schema=schema_list,
            params=params_map,
        )
        self._write(schedule)
        logger.info("Registered schedule %s (cron=%s)", schedule.id, schedule.cron)
        return schedule

    def get(self, schedule_id: str) -> Schedule | None:
        """Return the schedule for ``schedule_id`` or None if absent."""
        path = self._path_for(schedule_id)
        if not path.exists():
            return None
        return _load_schedule(path)

    def list(self) -> list[Schedule]:
        """Return all schedules in sorted-by-id order.

        Stable ordering matters for tests and for ``schedule list --json``
        consumers that diff-compare two operator hosts.
        """
        out: list[Schedule] = []
        for path in sorted(self._dir.glob("*.json")):
            schedule = _load_schedule(path)
            if schedule is not None:
                out.append(schedule)
        out.sort(key=lambda s: s.id)
        return out

    def remove(self, schedule_id: str) -> bool:
        """Remove a schedule. Returns True if it existed and was removed."""
        path = self._path_for(schedule_id)
        if not path.exists():
            return False
        path.unlink()
        logger.info("Removed schedule %s", schedule_id)
        return True

    def update_last_fire(self, schedule_id: str, fire_time: float) -> None:
        """Persist the latest fire timestamp for ``schedule_id``.

        Called from the supervisor after a fire is successfully dispatched
        and recorded in the audit chain. We persist last_fire_at to disk so
        a daemon restart can resume the catch-up policy from the last known
        fire (instead of treating every restart as a fresh start).
        """
        schedule = self.get(schedule_id)
        if schedule is None:
            return
        updated = Schedule(
            id=schedule.id,
            cron=schedule.cron,
            goal=schedule.goal,
            scenario_id=schedule.scenario_id,
            misfire_policy=schedule.misfire_policy,
            created_at=schedule.created_at,
            last_fire_at=fire_time,
            extra=schedule.extra.copy(),
            params_schema=[s.copy() for s in schedule.params_schema],
            params=schedule.params.copy(),
        )
        self._write(updated)

    def advance_fire_cursor(self, schedule_id: str, cursor_epoch: float) -> None:
        """Mark a fire window as processed *without* claiming it fired.

        Distinct from :meth:`update_last_fire` on purpose. ``last_fire_at`` is
        the operator-facing "last successful fire" and is rendered by doctor,
        status, and ``schedule list``; a window that a collision policy
        canceled produced no dispatch and no fire audit entry, so recording it
        there would print a fire that never happened.

        The cursor exists so the scheduler does not re-offer a window it has
        already decided about. Both are consulted when computing the next
        fire; only ``last_fire_at`` is reported.
        """
        schedule = self.get(schedule_id)
        if schedule is None:
            return
        extra = dict(schedule.extra)
        if float(extra.get(FIRE_CURSOR_KEY, 0.0)) >= cursor_epoch:
            return
        extra[FIRE_CURSOR_KEY] = cursor_epoch
        self._write(
            Schedule(
                id=schedule.id,
                cron=schedule.cron,
                goal=schedule.goal,
                scenario_id=schedule.scenario_id,
                misfire_policy=schedule.misfire_policy,
                created_at=schedule.created_at,
                last_fire_at=schedule.last_fire_at,
                extra=extra,
                params_schema=[s.copy() for s in schedule.params_schema],
                params=dict(schedule.params),
            ),
        )

    def _write(self, schedule: Schedule) -> None:
        """Write a schedule atomically to its JSON path.

        Atomic write via ``tmp + os.replace`` so a crash mid-write leaves
        either the old file or the new file intact; never half a
        document. The supervisor loads schedules on every tick and a torn
        write would either skip a fire (silent loss) or trip a JSON
        decode (loud, but breaks the supervisor).
        """
        path = self._path_for(schedule.id)
        tmp = path.with_suffix(path.suffix + ".tmp")
        payload = asdict(schedule)
        tmp.write_text(json.dumps(payload, sort_keys=True, indent=2))
        tmp.replace(path)


def _load_schedule(path: Path) -> Schedule | None:
    """Load a single schedule file. Returns None on parse error."""
    try:
        raw: Any = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Could not load schedule %s: %s", path, exc)
        return None
    if not isinstance(raw, dict):
        return None
    # ``json.loads`` returns ``Any``; narrow the dict's value type
    # explicitly so pyright doesn't propagate ``Unknown`` through every
    # call site.
    data: dict[str, Any] = {str(k): v for k, v in raw.items()}  # type: ignore[misc]
    try:
        misfire_raw = data.get("misfire_policy", "skip")
        misfire_policy: MisfirePolicy = "catch_up" if misfire_raw == "catch_up" else "skip"
        extra_raw = data.get("extra", {})
        extra: dict[str, Any] = (
            {str(k): v for k, v in extra_raw.items()}  # type: ignore[misc]
            if isinstance(extra_raw, dict)
            else {}
        )
        schema_raw = data.get("params_schema", [])
        params_schema: list[dict[str, Any]] = (
            [{str(k): v for k, v in row.items()} for row in schema_raw if isinstance(row, dict)]  # type: ignore[misc]
            if isinstance(schema_raw, list)
            else []
        )
        params_raw = data.get("params", {})
        params: dict[str, Any] = (
            {str(k): v for k, v in params_raw.items()}  # type: ignore[misc]
            if isinstance(params_raw, dict)
            else {}
        )
        return Schedule(
            id=str(data["id"]),
            cron=str(data["cron"]),
            goal=str(data.get("goal", "")),
            scenario_id=str(data.get("scenario_id", "")),
            misfire_policy=misfire_policy,
            created_at=float(data.get("created_at", 0.0)),
            last_fire_at=float(data.get("last_fire_at", 0.0)),
            extra=extra,
            params_schema=params_schema,
            params=params,
        )
    except (KeyError, TypeError, ValueError) as exc:
        logger.warning("Malformed schedule file %s: %s", path, exc)
        return None
