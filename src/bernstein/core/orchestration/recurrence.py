"""Deterministic recurrence canonicalisation for schedule projections.

Issue #2302. A recurring goal fire is a pure projection of
``(schedule_id, fire_time, last_state)`` onto a canonical task graph. The
*recurrence rule* that produced the fire instant - an RFC-5545 ``RRULE``
or a simple 5-field cron expression - is an input to that projection, not
an out-of-band trigger. Folding a canonical form of the rule into the
projection lets two operators prove they fired the byte-identical graph
under the byte-identical schedule definition, and lets a verifier tell a
daily digest apart from an hourly one from the projection hash alone.

The parser is a small in-tree subset, matching the discipline already
set by :func:`bernstein.core.planning.schedule_store.parse_cron`: no new
runtime dependency (the project bans ``python-dateutil`` /
``croniter`` in the wheelhouse path). We do **not** evaluate the rule
here - evaluation (when the supervisor next wakes) is separate from the
pure projection. We only *canonicalise* the rule text so that two
logically-equal rules serialise to the same bytes.

Supported forms:

- ``cron:<5-field expr>`` or a bare 5-field cron expression - validated
  via the existing cron parser and canonicalised to
  ``cron:<normalised>``.
- ``RRULE:FREQ=...;...`` (RFC-5545) - the part list is parsed, part
  names upper-cased, ``BY*`` value lists sorted numerically, and the
  parts re-emitted in a fixed canonical key order.

Determinism: every output is a pure function of the input string; no
wall-clock, no host locale, no set-iteration order leaks into the bytes.
"""

from __future__ import annotations

from bernstein.core.planning.schedule_store import CronParseError, parse_cron

#: Canonical ordering of RRULE parts. RFC-5545 does not mandate an order,
#: so two operators may write the same rule with parts in different
#: sequence; we re-emit in this fixed order so the canonical text - and
#: therefore the projection hash - does not depend on authoring order.
_RRULE_PART_ORDER: tuple[str, ...] = (
    "FREQ",
    "INTERVAL",
    "COUNT",
    "UNTIL",
    "WKST",
    "BYMONTH",
    "BYWEEKNO",
    "BYYEARDAY",
    "BYMONTHDAY",
    "BYDAY",
    "BYHOUR",
    "BYMINUTE",
    "BYSECOND",
    "BYSETPOS",
)

#: RRULE parts whose value is a comma-separated list we sort so member
#: order does not perturb the canonical text. ``BYDAY`` carries weekday
#: tokens (optionally signed, e.g. ``-1SU``); it is sorted lexically.
_RRULE_LIST_PARTS: frozenset[str] = frozenset(
    {
        "BYMONTH",
        "BYWEEKNO",
        "BYYEARDAY",
        "BYMONTHDAY",
        "BYDAY",
        "BYHOUR",
        "BYMINUTE",
        "BYSECOND",
        "BYSETPOS",
    }
)

#: The RFC-5545 frequency vocabulary we accept. Restricting the set keeps
#: an operator typo from silently producing a rule that never fires.
_VALID_FREQ: frozenset[str] = frozenset(
    {
        "SECONDLY",
        "MINUTELY",
        "HOURLY",
        "DAILY",
        "WEEKLY",
        "MONTHLY",
        "YEARLY",
    }
)

_VALID_WEEKDAYS: frozenset[str] = frozenset({"MO", "TU", "WE", "TH", "FR", "SA", "SU"})


class RecurrenceParseError(ValueError):
    """Raised when a recurrence rule cannot be parsed or canonicalised."""


def _canonicalise_rrule(rule: str) -> str:
    """Canonicalise an RFC-5545 RRULE string.

    The leading ``RRULE:`` prefix is optional on input and always present
    on output. Part names are upper-cased, ``BY*`` value lists are sorted
    (numerically where the members are integers, lexically otherwise),
    and parts are re-emitted in :data:`_RRULE_PART_ORDER`.

    Raises:
        RecurrenceParseError: On a missing/unknown ``FREQ``, a malformed
            part (no ``=``), a duplicate part, or an out-of-vocabulary
            weekday token.
    """
    body = rule[len("RRULE:") :] if rule.upper().startswith("RRULE:") else rule
    body = body.strip().rstrip(";")
    if not body:
        raise RecurrenceParseError("empty RRULE body")

    parts: dict[str, str] = {}
    for raw_part in body.split(";"):
        piece = raw_part.strip()
        if not piece:
            continue
        if "=" not in piece:
            raise RecurrenceParseError(f"malformed RRULE part {piece!r} (expected NAME=VALUE)")
        name, value = piece.split("=", 1)
        name = name.strip().upper()
        value = value.strip()
        if not name:
            raise RecurrenceParseError(f"empty RRULE part name in {piece!r}")
        if name in parts:
            raise RecurrenceParseError(f"duplicate RRULE part {name!r}")
        parts[name] = value

    freq = parts.get("FREQ", "").upper()
    if freq not in _VALID_FREQ:
        raise RecurrenceParseError(f"RRULE FREQ {parts.get('FREQ')!r} is not one of {sorted(_VALID_FREQ)}")
    parts["FREQ"] = freq

    for list_part in _RRULE_LIST_PARTS:
        if list_part not in parts:
            continue
        members = [m.strip().upper() for m in parts[list_part].split(",") if m.strip()]
        if not members:
            raise RecurrenceParseError(f"empty value list for RRULE part {list_part!r}")
        if list_part == "BYDAY":
            for member in members:
                token = member.lstrip("+-0123456789")
                if token not in _VALID_WEEKDAYS:
                    raise RecurrenceParseError(f"invalid BYDAY weekday token {member!r}")
        parts[list_part] = ",".join(_sort_members(members))

    if "WKST" in parts:
        wkst = parts["WKST"].upper()
        if wkst not in _VALID_WEEKDAYS:
            raise RecurrenceParseError(f"invalid WKST weekday {parts['WKST']!r}")
        parts["WKST"] = wkst

    ordered: list[str] = []
    seen: set[str] = set()
    for key in _RRULE_PART_ORDER:
        if key in parts:
            ordered.append(f"{key}={parts[key]}")
            seen.add(key)
    # Any part not in the canonical order list is appended sorted by name
    # so an unknown-but-valid RFC extension still serialises stably.
    for key in sorted(parts):
        if key not in seen:
            ordered.append(f"{key}={parts[key]}")
    return "RRULE:" + ";".join(ordered)


def _sort_members(members: list[str]) -> list[str]:
    """Sort a BY* value list numerically when integral, else lexically.

    Sorting keeps ``BYMONTHDAY=3,1,2`` and ``BYMONTHDAY=1,2,3`` on the
    same canonical bytes. Signed tokens (``BYSETPOS=-1``, ``BYDAY=-1SU``)
    are handled: pure integers sort by value, mixed lists fall back to a
    stable lexical sort so the function never raises on a well-formed but
    non-integer member.
    """

    def _is_int(token: str) -> bool:
        candidate = token[1:] if token[:1] in "+-" else token
        return candidate.isdigit()

    if all(_is_int(m) for m in members):
        return sorted(members, key=lambda m: (int(m), m))
    return sorted(members)


def canonicalise_recurrence(recurrence: str) -> str:
    """Return a canonical, order-stable form of a recurrence rule.

    Accepts either a cron expression (bare 5-field, or ``cron:`` prefixed)
    or an RFC-5545 ``RRULE``. The output is a byte-stable string suitable
    for folding into the deterministic projection: two operators who wrote
    the same rule in different token order land on identical bytes.

    An empty / whitespace-only input returns ``""`` so a projection with
    no declared recurrence stays byte-identical to a pre-#2302 projection.

    Args:
        recurrence: The rule text. ``""`` means "no recurrence declared".

    Returns:
        ``""`` for no rule, ``cron:<normalised expr>`` for cron, or
        ``RRULE:<canonical parts>`` for an RRULE.

    Raises:
        RecurrenceParseError: When a non-empty rule cannot be parsed.
    """
    text = recurrence.strip()
    if not text:
        return ""

    upper = text.upper()
    if upper.startswith(("RRULE:", "FREQ=")):
        return _canonicalise_rrule(text)

    expr = text[len("cron:") :].strip() if text.lower().startswith("cron:") else text
    try:
        parsed = parse_cron(expr)
    except CronParseError as exc:
        raise RecurrenceParseError(f"invalid cron expression {expr!r}: {exc}") from exc
    return "cron:" + parsed.raw


__all__ = [
    "RecurrenceParseError",
    "canonicalise_recurrence",
]
