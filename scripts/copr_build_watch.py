#!/usr/bin/env python3
"""Watch a Copr build to a verdict, on a deadline this job controls.

``copr-cli build`` can watch its own build, but its watch runs until the build
ends - however long Copr's public queue takes. When that outlives the job's
``timeout-minutes`` the step is cancelled, and a build that was merely slow
becomes indistinguishable from one that failed.

So the submission uses ``--nowait`` and this watcher owns the waiting:

* a build that reaches a terminal failure inside the window fails the job,
* a build that succeeds inside the window passes it, and
* a build still queued at the deadline is handed to
  ``.github/workflows/reconcile-release.yml``, which compares the published
  Copr version daily and opens a ``release-drift`` issue if it never lands.

The last case is not a swallowed failure: the submission already succeeded and
the outcome is unknown rather than bad. Failing there would paint releases red
for a public build queue.

Usage::

    python3 scripts/copr_build_watch.py --build-id 10802217 --deadline-seconds 900
"""

from __future__ import annotations

import argparse
import json
import time
import urllib.error
import urllib.request
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

API_URL = "https://copr.fedorainfracloud.org/api_3/build/{build_id}"
CHROOT_API_URL = "https://copr.fedorainfracloud.org/api_3/build-chroot/list?build_id={build_id}"
BUILD_URL = "https://copr.fedorainfracloud.org/coprs/build/{build_id}"
USER_AGENT = "bernstein-copr-build-watch"

# Copr reports one state for the whole build, and that state is `failed` when
# any single chroot failed. `publish.yml` deliberately submits to rawhide
# without gating the release on it: rawhide is a moving target whose churn
# costs more than it catches. fedora-45 is a pre-release Fedora whose
# interpreter (Python 3.15) lacks wheels for cbor2 and grpcio, so its failure
# is upstream churn, not a release defect; once it goes stable and wheels are
# available it should be removed from this list. Reading the aggregate state
# alone contradicts that, so a `failed` aggregate is re-read per chroot and
# only the gating ones decide the verdict.
NON_GATING_CHROOT_MARKERS = ("rawhide", "fedora-45")

SUCCESS_STATES = frozenset({"succeeded"})
# `skipped` means Copr already had this exact build and did not rebuild it. It
# is terminal and produces no new RPM, so it must not read as a publish.
FAILURE_STATES = frozenset({"failed", "canceled", "cancelled", "skipped"})

READ_ERRORS = (urllib.error.URLError, TimeoutError, ValueError, KeyError, OSError, TypeError)


def classify(state: str) -> str:
    """Return ``success``, ``failure`` or ``pending`` for a Copr build state.

    Unrecognised states count as ``pending``: Copr can add states, and reading
    an unknown one as terminal would invent a verdict nobody checked.
    """
    normalised = state.strip().lower()
    if normalised in SUCCESS_STATES:
        return "success"
    if normalised in FAILURE_STATES:
        return "failure"
    return "pending"


def is_gating_chroot(name: str) -> bool:
    """Return whether a chroot's outcome decides the release verdict."""
    lowered = name.strip().lower()
    return not any(marker in lowered for marker in NON_GATING_CHROOT_MARKERS)


def failed_gating_chroots(chroots: dict[str, str]) -> list[str]:
    """Return the gating chroots that did not succeed."""
    return sorted(name for name, state in chroots.items() if is_gating_chroot(name) and classify(state) == "failure")


def fetch_state(build_id: int) -> str:
    """Return the current Copr state for ``build_id``."""
    # Fixed https API host, no user-controlled scheme.
    request = urllib.request.Request(
        API_URL.format(build_id=build_id),
        headers={"User-Agent": USER_AGENT},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        payload = json.load(response)
    return str(payload["state"])


def fetch_chroots(build_id: int) -> dict[str, str]:
    """Return ``{chroot name: state}`` for ``build_id``."""
    # Fixed https API host, no user-controlled scheme.
    request = urllib.request.Request(
        CHROOT_API_URL.format(build_id=build_id),
        headers={"User-Agent": USER_AGENT},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        payload = json.load(response)
    items = payload if isinstance(payload, list) else payload["items"]
    return {str(item["name"]): str(item["state"]) for item in items}


def _verdict_from_chroots(
    build_id: int,
    build_url: str,
    *,
    fetch_chroot_states: Callable[[int], dict[str, str]],
) -> int:
    """Decide a `failed` aggregate by the gating chroots alone.

    Returns 0 when every failure sits in a non-gating chroot and at least one
    gating chroot published, 1 otherwise. An unreadable chroot list is a
    failure: an aggregate that says `failed` with no evidence about where is
    not something to wave through.
    """
    try:
        chroots = fetch_chroot_states(build_id)
    except READ_ERRORS as exc:
        print(f"::error::Copr build {build_id} failed and its chroots were unreadable ({exc}): {build_url}")
        return 1

    blocking = failed_gating_chroots(chroots)
    if blocking:
        print(f"::error::Copr build {build_id} failed on gating chroot(s) {', '.join(blocking)}: {build_url}")
        return 1

    published = sorted(
        name for name, state in chroots.items() if is_gating_chroot(name) and classify(state) == "success"
    )
    if not published:
        print(f"::error::Copr build {build_id} published no gating chroot: {build_url}")
        return 1

    tolerated = sorted(name for name in chroots if not is_gating_chroot(name))
    print(
        f"::notice::Copr build {build_id} published {len(published)} gating chroot(s) "
        f"({', '.join(published)}); non-gating {', '.join(tolerated)} did not build and "
        f"does not hold the release: {build_url}"
    )
    return 0


def watch(
    build_id: int,
    *,
    fetch: Callable[[int], str] = fetch_state,
    fetch_chroot_states: Callable[[int], dict[str, str]] = fetch_chroots,
    deadline_seconds: float = 900,
    poll_seconds: float = 30,
    time_fn: Callable[[], float] = time.monotonic,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> int:
    """Watch ``build_id`` until it settles or the deadline passes.

    Returns the process exit code: 0 when the build succeeded or is still
    running at the deadline, 1 when it ended in a terminal failure.
    """
    build_url = BUILD_URL.format(build_id=build_id)
    deadline = time_fn() + deadline_seconds
    state = "unknown"

    while True:
        try:
            state = fetch(build_id)
        except READ_ERRORS as exc:
            # One unreadable poll says nothing about the build.
            print(f"build {build_id}: could not read state ({exc})")
        else:
            print(f"build {build_id}: {state}")
            verdict = classify(state)
            if verdict == "success":
                print(f"Copr published build {build_id}: {build_url}")
                return 0
            if verdict == "failure":
                if state.strip().lower() == "failed":
                    return _verdict_from_chroots(build_id, build_url, fetch_chroot_states=fetch_chroot_states)
                print(f"::error::Copr build {build_id} ended in state '{state}': {build_url}")
                return 1

        remaining = deadline - time_fn()
        if remaining <= 0:
            print(
                f"::notice::Copr build {build_id} is still '{state}' after "
                f"{deadline_seconds:.0f}s: {build_url}. The submission succeeded; "
                "reconcile-release.yml compares the published Copr version daily "
                "and opens a release-drift issue if this version never lands."
            )
            return 0
        sleep_fn(min(poll_seconds, remaining))


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--build-id", required=True, type=int, help="Copr build id to watch")
    parser.add_argument(
        "--deadline-seconds",
        type=float,
        default=900,
        help="Stop watching after this many seconds (default: 900).",
    )
    parser.add_argument(
        "--poll-seconds",
        type=float,
        default=30,
        help="Seconds between polls (default: 30).",
    )
    args = parser.parse_args(argv)

    return watch(
        int(args.build_id),
        deadline_seconds=float(args.deadline_seconds),
        poll_seconds=float(args.poll_seconds),
    )


if __name__ == "__main__":
    raise SystemExit(main())
