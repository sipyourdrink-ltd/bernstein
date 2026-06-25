"""Criterion-aware retry budget with graceful degradation.

Synapse port (issue #1352).

The default retry path in ``task_retry.py`` rebooks an identical attempt:
same model, same prompt, same gate criteria.  When attempt #1 has already
exhausted the per-task budget, attempt #2 either burns the same amount
and yields the same failure, or trips the circuit breaker.  Operators
want the second attempt to be *cheaper and more cautious* - not a
rerun.

This module models that behaviour as a :class:`RetryBudget`: a finite
list of retries paired with an *ordered* list of criteria to dial down.
The first retry degrades the first criterion, the second retry degrades
the second criterion, and so on.  Once every criterion has been
degraded, further retries operate at the minimum bar - they are still
permitted but produce no additional degradation.

A criterion (:class:`Criterion`) is identified by name plus an integer
*level*.  Degrading a criterion decrements its level by ``1`` until it
reaches its declared minimum.  A criterion at its floor cannot be
degraded further; attempting to do so raises
:class:`CriterionExhaustedError`.

Public surface:

* :class:`Criterion` - name + level + min/max bounds.
* :class:`RetryBudget` - retries + ordered degradation policy.
* :class:`RetryDecision` - what to do for the *next* attempt.
* :func:`parse_retry_budget_spec` - parse the CLI string format
  ``"3 retries, degrade: coverage>tests>style"``.
* :exc:`RetryBudgetError` and subclasses.

The module has no runtime dependencies on the orchestrator: tests can
construct a budget directly and exercise the decision path without
spinning up a task store.  See ``tests/unit/test_retry_budget.py`` and
``tests/property/test_retry_budget_properties.py`` for examples.

This file is pyright-strict and ruff clean.  Mutating state lives in
``RetryBudget._criteria`` only; ``RetryDecision`` is a frozen snapshot.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import TYPE_CHECKING, Final, cast

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence

__all__ = [
    "DEFAULT_CRITERION_MAX",
    "DEFAULT_CRITERION_MIN",
    "Criterion",
    "CriterionExhaustedError",
    "DegradationKind",
    "DuplicateCriterionError",
    "RetryBudget",
    "RetryBudgetError",
    "RetryBudgetExhaustedError",
    "RetryDecision",
    "UnknownCriterionError",
    "parse_retry_budget_spec",
]


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


DEFAULT_CRITERION_MAX: Final[int] = 3
"""Default upper level for a criterion (e.g. ``"comprehensive"``)."""

DEFAULT_CRITERION_MIN: Final[int] = 0
"""Default lower level for a criterion (``"minimum viable"``)."""


class DegradationKind(StrEnum):
    """Kinds of degradation that a retry can apply.

    These are descriptive labels emitted in :class:`RetryDecision` so
    callers can render an operator-facing message.  The kind does not
    affect the numerical level - the criterion's *name* is what the
    orchestrator keys policy off.
    """

    LOWERED = "lowered"
    """The criterion was dialled down by one level."""

    FLOORED = "floored"
    """The criterion was already at its floor; no further degradation."""

    NONE = "none"
    """No degradation applied (e.g. the policy list is exhausted)."""


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class RetryBudgetError(Exception):
    """Base class for retry-budget errors."""


class UnknownCriterionError(RetryBudgetError):
    """The degradation policy referenced a criterion that doesn't exist."""

    def __init__(self, name: str, known: Iterable[str]) -> None:
        self.name = name
        self.known = tuple(known)
        super().__init__(f"Unknown criterion {name!r}; known criteria: {sorted(self.known)!r}")


class DuplicateCriterionError(RetryBudgetError):
    """A criterion appeared twice in the degradation policy."""

    def __init__(self, name: str) -> None:
        self.name = name
        super().__init__(f"Criterion {name!r} appears more than once in the policy")


class CriterionExhaustedError(RetryBudgetError):
    """Attempted to degrade a criterion already at its floor."""

    def __init__(self, criterion: Criterion) -> None:
        self.criterion = criterion
        super().__init__(f"Criterion {criterion.name!r} is already at its floor (level={criterion.level})")


class RetryBudgetExhaustedError(RetryBudgetError):
    """The retry budget has no attempts left."""

    def __init__(self) -> None:
        super().__init__("Retry budget is exhausted")


# ---------------------------------------------------------------------------
# Criterion
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Criterion:
    """A single quality criterion that can be dialled down.

    Attributes:
        name: Stable identifier (e.g. ``"coverage"``, ``"tests"``).
        level: Current level.  Higher = stricter.
        min_level: Floor.  Once reached, further degradation raises
            :exc:`CriterionExhaustedError`.
        max_level: Ceiling.  Used by :meth:`reset` to restore.
    """

    name: str
    level: int = DEFAULT_CRITERION_MAX
    min_level: int = DEFAULT_CRITERION_MIN
    max_level: int = DEFAULT_CRITERION_MAX

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("Criterion name must be non-empty")
        if self.min_level > self.max_level:
            raise ValueError(f"Criterion {self.name!r}: min_level ({self.min_level}) > max_level ({self.max_level})")
        if self.level < self.min_level or self.level > self.max_level:
            raise ValueError(
                f"Criterion {self.name!r}: level {self.level} outside [{self.min_level}, {self.max_level}]"
            )

    @property
    def is_at_floor(self) -> bool:
        """``True`` iff this criterion cannot be degraded any further."""
        return self.level <= self.min_level

    def degraded(self) -> Criterion:
        """Return a new criterion with the level decremented by one.

        Raises:
            CriterionExhaustedError: If the criterion is already at its
                floor.
        """
        if self.is_at_floor:
            raise CriterionExhaustedError(self)
        updated = cast(Criterion, replace(self, level=self.level - 1))
        return updated

    def reset(self) -> Criterion:
        """Return a new criterion restored to ``max_level``."""
        updated = cast(Criterion, replace(self, level=self.max_level))
        return updated


# ---------------------------------------------------------------------------
# Decision
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RetryDecision:
    """Snapshot describing the *next* retry attempt.

    Attributes:
        should_retry: ``True`` if the orchestrator should re-attempt.
            ``False`` means "give up / route to dead-letter".
        attempt_index: Zero-based index of the attempt this decision
            authorises.  Attempt 0 is the *first retry* (i.e. the
            second overall execution).
        degraded_criterion: The criterion whose level changed for this
            attempt, or ``None`` if no degradation was applied.
        degradation_kind: Why the criterion's level did or did not
            change.
        criteria_snapshot: Immutable view of all criteria as they stand
            *after* the degradation.
        reason: Operator-facing human-readable string.
    """

    should_retry: bool
    attempt_index: int
    degraded_criterion: Criterion | None
    degradation_kind: DegradationKind
    criteria_snapshot: tuple[Criterion, ...]
    reason: str

    def criterion(self, name: str) -> Criterion:
        """Return the criterion with ``name`` from the snapshot.

        Raises:
            UnknownCriterionError: If no such criterion exists.
        """
        for c in self.criteria_snapshot:
            if c.name == name:
                return c
        raise UnknownCriterionError(name, (c.name for c in self.criteria_snapshot))


# ---------------------------------------------------------------------------
# Budget
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class RetryBudget:
    """Criterion-aware retry budget.

    Construct with a positive integer ``retries`` and an ordered list of
    criteria names (``criterion_degradation``) describing *which*
    criterion to dial down on each successive retry.

    The constructor accepts either a list of :class:`Criterion`
    instances (full control over levels) *or* a list of names (default
    bounds).  Use :meth:`from_names` for the shorthand.

    Example::

        budget = RetryBudget(
            retries=3,
            criterion_degradation=[
                Criterion("coverage"),
                Criterion("tests"),
                Criterion("style"),
            ],
        )
        decision = budget.consume()
        assert decision.should_retry
        assert decision.degraded_criterion is not None
        assert decision.degraded_criterion.name == "coverage"

    Args:
        retries: Total number of retries permitted (>= 0).
        criterion_degradation: Ordered list of criteria to dial down,
            one per retry.  All names must be unique.

    Raises:
        ValueError: If ``retries`` is negative.
        DuplicateCriterionError: If a name appears twice.
    """

    retries: int
    criterion_degradation: Sequence[Criterion] = field(default_factory=list[Criterion])
    _attempts_used: int = field(default=0, init=False)
    _criteria: dict[str, Criterion] = field(default_factory=dict[str, Criterion], init=False)

    def __post_init__(self) -> None:
        if self.retries < 0:
            raise ValueError(f"retries must be >= 0 (got {self.retries})")
        # Build the canonical criteria map.  Duplicate detection happens
        # here so callers see the error eagerly, not on first retry.
        seen: set[str] = set()
        criteria: dict[str, Criterion] = {}
        for c in self.criterion_degradation:
            if c.name in seen:
                raise DuplicateCriterionError(c.name)
            seen.add(c.name)
            criteria[c.name] = c
        # Freeze the ordering so iteration is stable.
        self._criteria = criteria

    # ------------------------------------------------------------------
    # Alternate constructors
    # ------------------------------------------------------------------

    @classmethod
    def from_names(
        cls,
        retries: int,
        names: Sequence[str],
        *,
        max_level: int = DEFAULT_CRITERION_MAX,
        min_level: int = DEFAULT_CRITERION_MIN,
    ) -> RetryBudget:
        """Convenience: build a budget from criterion *names*.

        Each criterion is initialised at ``max_level`` with the supplied
        floor.

        Args:
            retries: Total retries (>= 0).
            names: Ordered criterion names.
            max_level: Starting level for every criterion.
            min_level: Floor for every criterion.

        Returns:
            A fresh :class:`RetryBudget`.
        """
        criteria = [Criterion(name=n, level=max_level, max_level=max_level, min_level=min_level) for n in names]
        return cls(retries=retries, criterion_degradation=criteria)

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------

    @property
    def attempts_used(self) -> int:
        """Number of retries already consumed."""
        return self._attempts_used

    @property
    def attempts_left(self) -> int:
        """Number of retries still available."""
        return max(0, self.retries - self._attempts_used)

    @property
    def is_exhausted(self) -> bool:
        """``True`` iff no further retries are permitted."""
        return self.attempts_left == 0

    @property
    def criteria(self) -> tuple[Criterion, ...]:
        """Snapshot of criteria preserving the configured order."""
        return tuple(self._criteria.values())

    def criterion(self, name: str) -> Criterion:
        """Return the current state of criterion ``name``.

        Raises:
            UnknownCriterionError: If no such criterion exists.
        """
        try:
            return self._criteria[name]
        except KeyError as exc:
            raise UnknownCriterionError(name, self._criteria.keys()) from exc

    # ------------------------------------------------------------------
    # Core operation
    # ------------------------------------------------------------------

    def peek(self) -> RetryDecision:
        """Compute the next decision *without* consuming a retry.

        Useful for previewing what would happen next (e.g. for logging
        or for tests).  The returned :class:`RetryDecision` reflects
        the criterion that *would* be degraded if :meth:`consume` were
        called.

        Returns:
            A :class:`RetryDecision` describing the next attempt.
        """
        return self._compute_decision(consume=False)

    def consume(self) -> RetryDecision:
        """Authorise the next retry and mutate state accordingly.

        On a successful call:

        * ``attempts_used`` is incremented.
        * The next criterion in the degradation policy is dialled down
          (if any remain and the criterion is not already at its
          floor).

        Returns:
            A :class:`RetryDecision`.  If the budget is exhausted the
            decision has ``should_retry=False`` and no state mutation
            occurs.
        """
        if self.is_exhausted:
            return RetryDecision(
                should_retry=False,
                attempt_index=self._attempts_used,
                degraded_criterion=None,
                degradation_kind=DegradationKind.NONE,
                criteria_snapshot=tuple(self._criteria.values()),
                reason="retry budget exhausted",
            )
        return self._compute_decision(consume=True)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _criterion_for_attempt(self, attempt_index: int) -> Criterion | None:
        """Return the criterion targeted by ``attempt_index`` (or None).

        The policy is consulted positionally: the first retry targets
        the first criterion, the second targets the second, etc.  If
        the policy is shorter than the attempt index, no criterion is
        targeted (we still permit the retry, just without degradation).
        """
        if attempt_index < 0 or attempt_index >= len(self.criterion_degradation):
            return None
        # Look up by name to get the *current* level - the configured
        # criterion is only the *target*; its mutable state lives in
        # ``self._criteria``.
        target_name = self.criterion_degradation[attempt_index].name
        return self._criteria[target_name]

    def _compute_decision(self, *, consume: bool) -> RetryDecision:
        """Shared decision logic for :meth:`peek` and :meth:`consume`."""
        attempt_index = self._attempts_used
        target = self._criterion_for_attempt(attempt_index)
        if target is None:
            # No criterion left in the policy at this index - retry is
            # still permitted but no degradation happens.
            if consume:
                self._attempts_used += 1
            return RetryDecision(
                should_retry=True,
                attempt_index=attempt_index,
                degraded_criterion=None,
                degradation_kind=DegradationKind.NONE,
                criteria_snapshot=tuple(self._criteria.values()),
                reason=(f"retry #{attempt_index + 1} of {self.retries} (no further criterion degradation configured)"),
            )
        if target.is_at_floor:
            # The criterion has already been degraded to its minimum on
            # a *prior* call (unusual, since the policy lists each
            # criterion at most once, but possible if a Criterion was
            # supplied with level == min_level).  Permit the retry but
            # do not crash.
            if consume:
                self._attempts_used += 1
            return RetryDecision(
                should_retry=True,
                attempt_index=attempt_index,
                degraded_criterion=target,
                degradation_kind=DegradationKind.FLOORED,
                criteria_snapshot=tuple(self._criteria.values()),
                reason=(
                    f"retry #{attempt_index + 1}: criterion {target.name!r} already at floor; no further degradation"
                ),
            )
        new_criterion = target.degraded()
        snapshot_map = self._criteria.copy()
        snapshot_map[new_criterion.name] = new_criterion
        if consume:
            self._criteria[new_criterion.name] = new_criterion
            self._attempts_used += 1
        return RetryDecision(
            should_retry=True,
            attempt_index=attempt_index,
            degraded_criterion=new_criterion,
            degradation_kind=DegradationKind.LOWERED,
            criteria_snapshot=tuple(snapshot_map.values()),
            reason=(f"retry #{attempt_index + 1}: degrading {new_criterion.name!r} to level {new_criterion.level}"),
        )

    # ------------------------------------------------------------------
    # Serialisation helpers
    # ------------------------------------------------------------------

    def to_dict(self) -> dict[str, object]:
        """Return a JSON-friendly representation."""
        return {
            "retries": self.retries,
            "attempts_used": self._attempts_used,
            "attempts_left": self.attempts_left,
            "criteria": [
                {
                    "name": c.name,
                    "level": c.level,
                    "min_level": c.min_level,
                    "max_level": c.max_level,
                }
                for c in self._criteria.values()
            ],
            "policy": [c.name for c in self.criterion_degradation],
        }


# ---------------------------------------------------------------------------
# CLI spec parser
# ---------------------------------------------------------------------------


# Accepted forms (case-insensitive on keywords):
#
#   "3 retries, degrade: coverage>tests>style"
#   "5, coverage > tests > style"
#   "1 retry, degrade: coverage"
#   "0"
#
# Whitespace around tokens is liberal.  The retry count is mandatory and
# must be the first token (integer).  The degradation list is optional
# and defaults to empty.

_RETRY_KEYWORDS: Final = ("retries", "retry", "attempts", "attempt")


def _is_criterion_name(name: str) -> bool:
    """Return True when *name* matches ``[A-Za-z_][A-Za-z0-9_-]*``."""
    if not name:
        return False
    first = name[0]
    if not (first == "_" or "A" <= first <= "Z" or "a" <= first <= "z"):
        return False
    return all(ch in ("_", "-") or "0" <= ch <= "9" or "A" <= ch <= "Z" or "a" <= ch <= "z" for ch in name[1:])


def _split_retry_budget_spec(spec: str) -> tuple[int, str | None]:
    """Split a retry-budget CLI spec into retry count and optional policy."""
    digit_end = 0
    while digit_end < len(spec) and spec[digit_end].isdigit():
        digit_end += 1
    if digit_end == 0:
        raise ValueError(f"could not parse retry budget spec: {spec!r}")

    retries = int(spec[:digit_end])
    remainder = spec[digit_end:].lstrip()
    lowered = remainder.lower()
    for keyword in _RETRY_KEYWORDS:
        if lowered.startswith(keyword):
            remainder = remainder[len(keyword) :].lstrip()
            break

    if not remainder:
        return retries, None
    if remainder[0] not in {",", ";"}:
        raise ValueError(f"could not parse retry budget spec: {spec!r}")

    policy = remainder[1:].lstrip()
    if policy.lower().startswith("degrade"):
        after_keyword = policy[len("degrade") :].lstrip()
        if after_keyword.startswith(":"):
            policy = after_keyword[1:].lstrip()

    policy = policy.strip()
    if not policy or "," in policy or ";" in policy:
        raise ValueError(f"could not parse retry budget spec: {spec!r}")
    return retries, policy


def parse_retry_budget_spec(
    spec: str,
    *,
    known_criteria: Mapping[str, Criterion] | None = None,
) -> RetryBudget:
    """Parse the CLI ``--retry-budget`` argument into a :class:`RetryBudget`.

    The format mirrors the issue description:

        ``"3 retries, degrade: coverage>tests>style"``

    Either commas or semicolons can separate the retry count from the
    policy.  The ``"degrade:"`` keyword is optional.  Spaces around
    ``">"`` are allowed.  Criterion names match
    ``[A-Za-z_][A-Za-z0-9_-]*``.

    Args:
        spec: Raw operator-supplied string.
        known_criteria: Optional whitelist.  If provided, every name in
            the policy must appear in the mapping; the mapping's
            :class:`Criterion` values are used (so callers can specify
            non-default bounds).  If omitted, criteria are constructed
            with default bounds.

    Returns:
        A :class:`RetryBudget`.

    Raises:
        ValueError: If the spec is syntactically invalid.
        UnknownCriterionError: If ``known_criteria`` is supplied and a
            name doesn't appear in it.
        DuplicateCriterionError: If a criterion is listed twice.
    """
    stripped = spec.strip()
    if not stripped:
        raise ValueError("retry budget spec is empty")
    retries, raw_policy = _split_retry_budget_spec(stripped)
    criteria: list[Criterion] = []
    if raw_policy is not None:
        seen: set[str] = set()
        for raw_name in raw_policy.split(">"):
            name = raw_name.strip()
            if not name:
                raise ValueError(f"empty criterion name in retry budget spec: {spec!r}")
            if not _is_criterion_name(name):
                raise ValueError(f"invalid criterion name {name!r} in retry budget spec")
            if name in seen:
                raise DuplicateCriterionError(name)
            seen.add(name)
            if known_criteria is not None:
                if name not in known_criteria:
                    raise UnknownCriterionError(name, known_criteria.keys())
                criteria.append(known_criteria[name])
            else:
                criteria.append(Criterion(name=name))
    return RetryBudget(retries=retries, criterion_degradation=criteria)
