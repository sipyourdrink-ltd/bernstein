"""Schedule kinds and host-independent local-time fire resolution.

Issue #2546. A registered recipe declares *when* it fires with one of three
schedule kinds, and it may declare an IANA timezone so a "9am local" or a
"last weekday of the month" schedule behaves identically on every host,
including across a DST transition.

The load-bearing property is determinism across hosts: two operators whose
machines sit in *different system timezones* must resolve the same local
wall-clock instant to the byte-identical Unix epoch. This module reaches
that by never touching the host locale - every conversion goes through
:class:`zoneinfo.ZoneInfo` (stdlib, no new runtime dependency), which
carries the zone rules explicitly. ``time.localtime`` / naive
``datetime.now`` never enter the path.

DST boundaries are the two places a naive conversion silently forks:

- **Spring-forward gap** - a wall time like ``02:30`` on the transition day
  does not exist. ``datetime.timestamp`` would pick a host-dependent
  offset. We instead resolve it explicitly by the declared policy.
- **Fall-back overlap** - a wall time like ``01:30`` happens twice. PEP 495
  ``fold`` disambiguates it; the declared policy selects ``fold``.

Because the resolution is a pure function of ``(tz, naive wall time,
policy)`` and never reads the host clock or locale, both operators land on
the same epoch. The epoch then feeds the existing pure projection
(:func:`bernstein.core.orchestration.schedule_projection.project_schedule_fire`),
which already binds the instant into the graph hash.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from enum import StrEnum
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

__all__ = [
    "DstPolicy",
    "ScheduleKind",
    "ScheduleKindError",
    "canonical_timezone",
    "interval_anchor_fires",
    "is_ambiguous_local",
    "is_imaginary_local",
    "resolve_local_instant",
]

#: Whole-second offset used to probe a transition boundary.
_UTC = ZoneInfo("UTC")


class ScheduleKindError(ValueError):
    """Raised when a schedule kind or its parameters are malformed."""


class ScheduleKind(StrEnum):
    """The declared shape of a recipe schedule.

    ``CRON`` reuses the existing 5-field parser; ``INTERVAL_ANCHOR`` fires
    every N seconds measured from a fixed anchor epoch (so two operators
    agree on the grid without a shared "now"); ``RRULE`` reuses the
    existing RFC-5545 canonicaliser. The kind is part of the canonical
    recipe body, so a change of kind is a change of definition hash.
    """

    CRON = "cron"
    INTERVAL_ANCHOR = "interval_anchor"
    RRULE = "rrule"


class DstPolicy(StrEnum):
    """How a schedule resolves a DST-ambiguous or DST-imaginary local time.

    The policy is part of the canonical recipe body, so two hosts resolving
    the same transition prove the identical decision from the hash alone.

    - ``PRE_TRANSITION`` - the conservative "earlier" reading: an ambiguous
      fall-back time resolves to its first (pre-transition) occurrence, and
      an imaginary spring-forward time resolves to the instant just before
      the gap opens.
    - ``POST_TRANSITION`` - the "later" reading: an ambiguous time resolves
      to its second (post-transition) occurrence, and an imaginary time
      resolves to the instant the clock jumps to after the gap.
    - ``STRICT`` - refuse to guess: an ambiguous or imaginary local time
      raises :class:`ScheduleKindError` so a misdeclared calendar rule
      fails loudly instead of firing at a surprising instant.
    """

    PRE_TRANSITION = "pre_transition"
    POST_TRANSITION = "post_transition"
    STRICT = "strict"


def canonical_timezone(tz_name: str) -> str:
    """Validate *tz_name* as an IANA zone and return it unchanged.

    An empty string means "UTC / no local-time semantics declared" and is
    returned as ``""`` so a recipe without a timezone stays byte-identical
    to a pre-#2546 definition.

    Raises:
        ScheduleKindError: When ``tz_name`` is not a resolvable IANA zone.
            Validating at canonicalisation time keeps a typo out of the
            content hash (a bad zone must never seal into a definition).
    """
    name = tz_name.strip()
    if not name:
        return ""
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ScheduleKindError(f"unknown IANA timezone {tz_name!r}: {exc}") from exc
    return name


def is_ambiguous_local(zone: ZoneInfo, naive: datetime) -> bool:
    """Return True when *naive* wall time occurs twice in *zone* (fall-back)."""
    earlier = naive.replace(tzinfo=zone, fold=0)
    later = naive.replace(tzinfo=zone, fold=1)
    return earlier.utcoffset() != later.utcoffset()


def is_imaginary_local(zone: ZoneInfo, naive: datetime) -> bool:
    """Return True when *naive* wall time does not exist in *zone* (gap).

    Uses the PEP 495 round-trip test: an imaginary time does not survive a
    round trip through UTC, because the local clock skipped over it.
    """
    round_trip = naive.replace(tzinfo=zone, fold=0).astimezone(_UTC).astimezone(zone)
    return round_trip.replace(tzinfo=None) != naive


def resolve_local_instant(
    *,
    tz_name: str,
    naive_local: datetime,
    dst_policy: DstPolicy | str = DstPolicy.PRE_TRANSITION,
) -> int:
    """Resolve a naive local wall time to a Unix epoch, host-independently.

    Pure function: the result depends only on ``(tz_name, naive_local,
    dst_policy)`` and the stdlib zone database, never on the host locale or
    clock. Two operators in different system timezones therefore compute
    the byte-identical epoch for the same declared local instant.

    Args:
        tz_name: IANA zone (``""`` means UTC).
        naive_local: A timezone-naive ``datetime`` giving the intended wall
            time in ``tz_name``.
        dst_policy: How to resolve an ambiguous / imaginary time (see
            :class:`DstPolicy`).

    Returns:
        Integer Unix epoch seconds of the resolved instant.

    Raises:
        ScheduleKindError: When ``naive_local`` carries a tzinfo, when the
            policy is ``STRICT`` and the time is ambiguous or imaginary, or
            when ``tz_name`` is not a resolvable zone.
    """
    if naive_local.tzinfo is not None:
        raise ScheduleKindError("naive_local must be timezone-naive; the zone is declared separately")
    policy = DstPolicy(dst_policy)
    name = canonical_timezone(tz_name)
    zone = _UTC if not name else ZoneInfo(name)

    imaginary = is_imaginary_local(zone, naive_local)
    ambiguous = is_ambiguous_local(zone, naive_local)

    if policy is DstPolicy.STRICT and (imaginary or ambiguous):
        kind = "imaginary (spring-forward gap)" if imaginary else "ambiguous (fall-back overlap)"
        raise ScheduleKindError(
            f"local time {naive_local.isoformat()} in {name or 'UTC'} is {kind}; "
            "declare PRE_TRANSITION or POST_TRANSITION to resolve it deterministically",
        )

    if imaginary:
        # The wall time was skipped. Probe the offsets on either side of the
        # gap and shift the instant to the chosen edge. ``fold=0`` reads the
        # pre-gap offset, ``fold=1`` the post-gap offset.
        pre = naive_local.replace(tzinfo=zone, fold=0)
        post = naive_local.replace(tzinfo=zone, fold=1)
        gap = post.utcoffset() - pre.utcoffset()  # negative in spring-forward
        if policy is DstPolicy.POST_TRANSITION:
            resolved = naive_local.replace(tzinfo=zone, fold=1) - gap
        else:
            # PRE_TRANSITION: the instant just before the gap opens.
            resolved = (naive_local.replace(tzinfo=zone, fold=0) - gap) - timedelta(seconds=1)
        return int(resolved.astimezone(_UTC).timestamp())

    fold = 1 if policy is DstPolicy.POST_TRANSITION else 0
    aware = naive_local.replace(tzinfo=zone, fold=fold)
    return int(aware.timestamp())


def interval_anchor_fires(
    *,
    anchor_epoch: int,
    interval_seconds: int,
    after_epoch: int,
) -> int:
    """Return the first ``anchor + k*interval`` fire strictly after *after_epoch*.

    Deterministic grid: the fire instants are a pure function of
    ``(anchor_epoch, interval_seconds)``, so two operators land on the same
    grid without needing a shared wall clock. The projection then binds the
    instant into the graph hash exactly as a cron fire does.

    Raises:
        ScheduleKindError: When ``interval_seconds`` is not positive.
    """
    if interval_seconds <= 0:
        raise ScheduleKindError(f"interval_seconds must be positive, got {interval_seconds}")
    if after_epoch < anchor_epoch:
        return anchor_epoch
    elapsed = after_epoch - anchor_epoch
    steps = (elapsed // interval_seconds) + 1
    return anchor_epoch + steps * interval_seconds
