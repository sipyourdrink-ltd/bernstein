"""Persistent donor budget policy for volunteer claims.

The usage budget in :mod:`bernstein.cli.usage_provisioning` is a daily,
project-local throttle. A volunteer budget is deliberately different: it is
durable volunteer run state, does not reset at midnight, and reserves estimated
tokens before a claim so a restart cannot silently offer the same capacity twice.
"""

from __future__ import annotations

import json
import math
import os
import tempfile
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, TypedDict, cast

if TYPE_CHECKING:
    from collections.abc import Iterable

    from bernstein.adapters.capability_profile import AdapterCapabilityProfile

LEDGER_SCHEMA_VERSION = 1
DEFAULT_BUDGET_DIR = Path(".sdd") / "runtime" / "volunteer" / "budget"
DEFAULT_BUDGET_CONFIG_PATH = DEFAULT_BUDGET_DIR / "config.yaml"
DEFAULT_LEDGER_PATH = DEFAULT_BUDGET_DIR / "ledger.json"

_SIZE_ORDER = {"xs": 0, "s": 1, "m": 2, "l": 3, "xl": 4}

try:
    import yaml as _yaml
except ImportError:
    _yaml = None  # type: ignore[assignment]


class BudgetLedgerError(ValueError):
    """The persisted ledger cannot be read without weakening a donor limit."""


class BudgetConfigError(ValueError):
    """The donor budget configuration is malformed or cannot be read."""


class BudgetClaimError(RuntimeError):
    """A claim cannot be reserved under the current donor budget."""

    def __init__(self, refusal: ClaimRefusal) -> None:
        super().__init__(refusal.detail)
        self.refusal = refusal


class BudgetLineItem(TypedDict):
    """One auditable dimension in a run receipt."""

    dimension: str
    unit: str
    authorized: int | float | None
    used: int | float
    reserved: int | float
    remaining: int | float | None


@dataclass(frozen=True, slots=True)
class VolunteerBudget:
    """Limits a donor authorizes across volunteer tasks on one machine."""

    max_tasks: int | None = None
    max_hours: float | None = None
    max_tokens: int | None = None
    max_size: str | None = None
    local_only: bool = False

    def __post_init__(self) -> None:
        if self.max_tasks is not None and self.max_tasks < 0:
            raise ValueError("max_tasks must be non-negative")
        if self.max_hours is not None and (self.max_hours < 0 or not math.isfinite(self.max_hours)):
            raise ValueError("max_hours must be finite and non-negative")
        if self.max_tokens is not None and self.max_tokens < 0:
            raise ValueError("max_tokens must be non-negative")
        if self.max_size is not None and _normalise_size(self.max_size) not in {"xs", "s", "m"}:
            raise ValueError("max_size must be one of: xs, s, m")

    def to_dict(self) -> dict[str, int | float | str | bool | None]:
        """Return the stable donor configuration shape."""
        return {
            "max_tasks": self.max_tasks,
            "max_hours": self.max_hours,
            "max_tokens": self.max_tokens,
            "max_size": _normalise_size(self.max_size) if self.max_size is not None else None,
            "local_only": self.local_only,
        }


@dataclass(frozen=True, slots=True)
class BudgetReservation:
    """Capacity reserved before a task is claimed."""

    claim_id: str
    token_estimate: int
    wall_clock_hours: float = 0.0

    def __post_init__(self) -> None:
        if not self.claim_id.strip():
            raise ValueError("claim_id must not be empty")
        if self.token_estimate < 0:
            raise ValueError("token_estimate must be non-negative")
        if self.wall_clock_hours < 0 or not math.isfinite(self.wall_clock_hours):
            raise ValueError("wall_clock_hours must be finite and non-negative")


@dataclass(frozen=True, slots=True)
class BudgetLedger:
    """Permanent donor consumption plus claims currently in flight."""

    tasks_used: int = 0
    hours_used: float = 0.0
    tokens_used_estimate: int = 0
    tokens_used_actual: int = 0
    reservations: tuple[BudgetReservation, ...] = ()

    def __post_init__(self) -> None:
        if self.tasks_used < 0:
            raise ValueError("tasks_used must be non-negative")
        if self.hours_used < 0 or not math.isfinite(self.hours_used):
            raise ValueError("hours_used must be finite and non-negative")
        if self.tokens_used_estimate < 0 or self.tokens_used_actual < 0:
            raise ValueError("token totals must be non-negative")
        claim_ids = [reservation.claim_id for reservation in self.reservations]
        if len(claim_ids) != len(set(claim_ids)):
            raise ValueError("reservation claim_id values must be unique")

    @property
    def tasks_reserved(self) -> int:
        """Number of tasks already claimed but not terminal."""
        return len(self.reservations)

    @property
    def tokens_reserved(self) -> int:
        """Estimated tokens held for tasks already in flight."""
        return sum(reservation.token_estimate for reservation in self.reservations)

    @property
    def hours_reserved(self) -> float:
        """Estimated wall clock held for tasks already in flight."""
        return sum(reservation.wall_clock_hours for reservation in self.reservations)

    def to_dict(self) -> dict[str, object]:
        """Return the versioned JSON representation persisted on disk."""
        return {
            "schema_version": LEDGER_SCHEMA_VERSION,
            "tasks_used": self.tasks_used,
            "hours_used": self.hours_used,
            "tokens_used_estimate": self.tokens_used_estimate,
            "tokens_used_actual": self.tokens_used_actual,
            "reservations": [
                {
                    "claim_id": reservation.claim_id,
                    "token_estimate": reservation.token_estimate,
                    "wall_clock_hours": reservation.wall_clock_hours,
                }
                for reservation in self.reservations
            ],
        }


@dataclass(frozen=True, slots=True)
class BudgetRemaining:
    """Capacity still available after completed and reserved work."""

    tasks: int | None
    hours: float | None
    tokens: int | None


@dataclass(frozen=True, slots=True)
class ClaimRefusal:
    """A machine-readable reason a new claim cannot start."""

    reason: str
    detail: str


def load_budget_config(path: Path = DEFAULT_BUDGET_CONFIG_PATH) -> VolunteerBudget | None:
    """Load donor limits from YAML, accepting JSON when PyYAML is absent."""
    if not path.exists():
        return None
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as error:
        raise BudgetConfigError(f"cannot read volunteer budget config {path}: {error}") from error
    if _yaml is not None:
        try:
            raw = cast(object, _yaml.safe_load(text))
        except _yaml.YAMLError as error:
            raise BudgetConfigError(f"cannot parse volunteer budget config {path}: {error}") from error
    else:
        try:
            raw = cast(object, json.loads(text))
        except json.JSONDecodeError as error:
            raise BudgetConfigError(f"cannot parse volunteer budget config {path}: {error}") from error
    if not isinstance(raw, dict):
        raise BudgetConfigError(f"volunteer budget config {path} is not an object")
    values = cast(dict[str, object], raw)
    try:
        return VolunteerBudget(
            max_tasks=_optional_int(values.get("max_tasks")),
            max_hours=_optional_float(values.get("max_hours")),
            max_tokens=_optional_int(values.get("max_tokens")),
            max_size=_optional_str(values.get("max_size")),
            local_only=_optional_bool(values.get("local_only", False)),
        )
    except (TypeError, ValueError) as error:
        raise BudgetConfigError(f"invalid volunteer budget config {path}: {error}") from error


def save_budget_config(budget: VolunteerBudget, path: Path = DEFAULT_BUDGET_CONFIG_PATH) -> None:
    """Atomically persist donor limits beside the durable volunteer run state."""
    payload = budget.to_dict()
    if _yaml is not None:
        text = _yaml.safe_dump(payload, sort_keys=True)
    else:
        text = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    _atomic_write_text(path, text)


def with_budget_overrides(
    budget: VolunteerBudget | None,
    *,
    max_tasks: int | None = None,
    max_hours: float | None = None,
    max_tokens: int | None = None,
    max_size: str | None = None,
    local_only: bool | None = None,
) -> VolunteerBudget:
    """Apply explicit CLI values over persisted preferences."""
    base = budget or VolunteerBudget()
    return VolunteerBudget(
        max_tasks=base.max_tasks if max_tasks is None else max_tasks,
        max_hours=base.max_hours if max_hours is None else max_hours,
        max_tokens=base.max_tokens if max_tokens is None else max_tokens,
        max_size=base.max_size if max_size is None else max_size,
        local_only=base.local_only if local_only is None else local_only,
    )


def load_ledger(path: Path = DEFAULT_LEDGER_PATH) -> BudgetLedger:
    """Load a ledger, returning a fresh one only when no file exists.

    Malformed state raises :class:`BudgetLedgerError`: silently replacing it
    with an empty ledger would restore capacity the donor already spent.
    """
    if not path.exists():
        return BudgetLedger()
    try:
        raw = cast(object, json.loads(path.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError) as error:
        raise BudgetLedgerError(f"cannot read volunteer budget ledger {path}: {error}") from error
    if not isinstance(raw, dict):
        raise BudgetLedgerError(f"volunteer budget ledger {path} is not a JSON object")
    values = cast(dict[str, object], raw)
    if values.get("schema_version") != LEDGER_SCHEMA_VERSION:
        raise BudgetLedgerError(f"unsupported volunteer budget ledger schema: {values.get('schema_version')!r}")
    reservations_raw = values.get("reservations", [])
    if not isinstance(reservations_raw, list):
        raise BudgetLedgerError("volunteer budget ledger reservations must be a list")
    reservation_values = cast(list[object], reservations_raw)
    try:
        reservations = tuple(_reservation_from_object(item) for item in reservation_values)
        return BudgetLedger(
            tasks_used=_ledger_int(values.get("tasks_used", 0), "tasks_used"),
            hours_used=_ledger_float(values.get("hours_used", 0.0), "hours_used"),
            tokens_used_estimate=_ledger_int(values.get("tokens_used_estimate", 0), "tokens_used_estimate"),
            tokens_used_actual=_ledger_int(values.get("tokens_used_actual", 0), "tokens_used_actual"),
            reservations=reservations,
        )
    except (KeyError, TypeError, ValueError) as error:
        if isinstance(error, BudgetLedgerError):
            raise
        raise BudgetLedgerError(f"invalid volunteer budget ledger {path}: {error}") from error


def save_ledger(ledger: BudgetLedger, path: Path = DEFAULT_LEDGER_PATH) -> None:
    """Atomically persist *ledger* using a same-directory temporary file."""
    _atomic_write_text(path, json.dumps(ledger.to_dict(), indent=2, sort_keys=True) + "\n")


def _atomic_write_text(path: Path, text: str) -> None:
    """Write text through a flushed same-directory temporary and replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as stream:
            temporary = Path(stream.name)
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except OSError:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        raise


def remaining(budget: VolunteerBudget, ledger: BudgetLedger) -> BudgetRemaining:
    """Return donor capacity after terminal usage and in-flight reserves."""
    tasks = None
    if budget.max_tasks is not None:
        tasks = max(0, budget.max_tasks - ledger.tasks_used - ledger.tasks_reserved)
    hours = None
    if budget.max_hours is not None:
        hours = max(0.0, budget.max_hours - ledger.hours_used - ledger.hours_reserved)
    tokens = None
    if budget.max_tokens is not None:
        tokens = max(0, budget.max_tokens - ledger.tokens_used_actual - ledger.tokens_reserved)
    return BudgetRemaining(tasks=tasks, hours=hours, tokens=tokens)


def remaining_wall_clock_minutes(budget: VolunteerBudget, ledger: BudgetLedger) -> int | None:
    """Translate remaining donor hours for ``build_volunteer_profile``."""
    hours = remaining(budget, ledger).hours
    return None if hours is None else int(hours * 60)


def refuses_claim(
    budget: VolunteerBudget,
    ledger: BudgetLedger,
    *,
    task_size: str,
    token_estimate: int,
    wall_clock_hours: float = 0.0,
    adapter_profile: AdapterCapabilityProfile | None = None,
) -> ClaimRefusal | None:
    """Return why a new claim is outside budget, or ``None`` when admitted."""
    if token_estimate < 0:
        raise ValueError("token_estimate must be non-negative")
    if wall_clock_hours < 0 or not math.isfinite(wall_clock_hours):
        raise ValueError("wall_clock_hours must be finite and non-negative")

    available = remaining(budget, ledger)
    if available.tasks is not None and available.tasks < 1:
        return ClaimRefusal("task_budget_exhausted", "the authorized task budget is exhausted")
    if available.hours is not None and (available.hours <= 0 or wall_clock_hours > available.hours):
        return ClaimRefusal(
            "wall_clock_budget_exhausted",
            f"the task needs {wall_clock_hours:g} hours but only {available.hours:g} authorized hours remain",
        )
    if available.tokens is not None and token_estimate > available.tokens:
        return ClaimRefusal(
            "token_budget_exhausted",
            f"the task estimates {token_estimate} tokens but only {available.tokens} authorized tokens remain",
        )

    normal_size = _normalise_size(task_size)
    if normal_size not in _SIZE_ORDER:
        return ClaimRefusal("task_size_unknown", f"task size {task_size!r} is not a recognized size label")
    if budget.max_size is not None and _SIZE_ORDER[normal_size] > _SIZE_ORDER[_normalise_size(budget.max_size)]:
        return ClaimRefusal(
            "size_cap_exceeded",
            f"task size/{normal_size} exceeds the donor's size/{_normalise_size(budget.max_size)} cap",
        )
    if budget.local_only and (adapter_profile is None or not adapter_profile.local_models):
        return ClaimRefusal(
            "local_only_adapter_required",
            "the donor requires an adapter capability profile with local-model support",
        )
    return None


def reserve_claim(
    budget: VolunteerBudget,
    ledger: BudgetLedger,
    *,
    claim_id: str,
    task_size: str,
    token_estimate: int,
    wall_clock_hours: float = 0.0,
    adapter_profile: AdapterCapabilityProfile | None = None,
) -> BudgetLedger:
    """Reserve a claim's task slot and token estimate before external claim."""
    if any(reservation.claim_id == claim_id for reservation in ledger.reservations):
        return ledger
    refusal = refuses_claim(
        budget,
        ledger,
        task_size=task_size,
        token_estimate=token_estimate,
        wall_clock_hours=wall_clock_hours,
        adapter_profile=adapter_profile,
    )
    if refusal is not None:
        raise BudgetClaimError(refusal)
    reservation = BudgetReservation(
        claim_id=claim_id,
        token_estimate=token_estimate,
        wall_clock_hours=wall_clock_hours,
    )
    return replace(ledger, reservations=(*ledger.reservations, reservation))


def complete_claim(
    ledger: BudgetLedger,
    *,
    claim_id: str,
    hours: float,
    actual_tokens: int,
) -> BudgetLedger:
    """Record a completed or aborted in-flight task without rechecking caps.

    The absence of a budget argument is intentional: a task admitted earlier
    is allowed to reach a terminal state even if it spent the final capacity.
    """
    if hours < 0 or not math.isfinite(hours) or actual_tokens < 0:
        raise ValueError("completion usage must be finite and non-negative")
    reservation = next((item for item in ledger.reservations if item.claim_id == claim_id), None)
    if reservation is None:
        raise BudgetLedgerError(f"no in-flight budget reservation for {claim_id!r}")
    return BudgetLedger(
        tasks_used=ledger.tasks_used + 1,
        hours_used=ledger.hours_used + hours,
        tokens_used_estimate=ledger.tokens_used_estimate + reservation.token_estimate,
        tokens_used_actual=ledger.tokens_used_actual + actual_tokens,
        reservations=tuple(item for item in ledger.reservations if item.claim_id != claim_id),
    )


def filter_local_profiles(
    budget: VolunteerBudget,
    profiles: Iterable[tuple[str, AdapterCapabilityProfile]],
) -> list[tuple[str, AdapterCapabilityProfile]]:
    """Filter adapter profiles through their canonical ``local_models`` flag."""
    candidates = list(profiles)
    if not budget.local_only:
        return candidates
    return [(name, profile) for name, profile in candidates if profile.local_models]


def budget_line_items(budget: VolunteerBudget, ledger: BudgetLedger) -> list[BudgetLineItem]:
    """Build the stable line-item shape embedded in volunteer receipts."""
    available = remaining(budget, ledger)
    return [
        {
            "dimension": "tasks",
            "unit": "tasks",
            "authorized": budget.max_tasks,
            "used": ledger.tasks_used,
            "reserved": ledger.tasks_reserved,
            "remaining": available.tasks,
        },
        {
            "dimension": "wall_clock",
            "unit": "hours",
            "authorized": budget.max_hours,
            "used": ledger.hours_used,
            "reserved": ledger.hours_reserved,
            "remaining": available.hours,
        },
        {
            "dimension": "tokens",
            "unit": "tokens",
            "authorized": budget.max_tokens,
            "used": ledger.tokens_used_actual,
            "reserved": ledger.tokens_reserved,
            "remaining": available.tokens,
        },
    ]


def _normalise_size(value: str) -> str:
    return value.strip().lower().removeprefix("size/")


def _optional_int(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise TypeError("integer budget must be an integer")
    return int(value)


def _optional_float(value: object) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise TypeError("numeric budget must be a number")
    return float(value)


def _optional_str(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError("max_size must be a string")
    return value


def _optional_bool(value: object) -> bool:
    if not isinstance(value, bool):
        raise TypeError("local_only must be a boolean")
    return value


def _reservation_from_object(value: object) -> BudgetReservation:
    if not isinstance(value, dict):
        raise BudgetLedgerError("volunteer budget ledger contains a malformed reservation")
    item = cast(dict[str, object], value)
    claim_id = item.get("claim_id")
    if not isinstance(claim_id, str):
        raise BudgetLedgerError("reservation claim_id must be a string")
    return BudgetReservation(
        claim_id=claim_id,
        token_estimate=_ledger_int(item.get("token_estimate"), "token_estimate"),
        wall_clock_hours=_ledger_float(item.get("wall_clock_hours", 0.0), "wall_clock_hours"),
    )


def _ledger_int(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise BudgetLedgerError(f"ledger {field} must be an integer")
    return value


def _ledger_float(value: object, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise BudgetLedgerError(f"ledger {field} must be numeric")
    return float(value)
