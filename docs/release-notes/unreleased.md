# Unreleased

Changes merged to `main` that are not yet part of a tagged release. Each
tagged release has its own page in this directory; this page carries what has
landed since the newest one.

Cutting a version empties this page: every entry the tag ships moves onto that
version's page in the release PR itself. `tests/unit/test_unreleased_notes_rotation.py`
holds the page to that — an entry naming an issue or PR a tagged release page
already documents fails the build. An entry that cites released work as context
rather than as its own attribution is exempted by hand there, with the reason.


## Nightly dependency audit is green again

The nightly full-closure audit runs `pip-audit --strict` over the dev closure and had failed since 2026-08-22 on `pip` 26.1.2 (PYSEC-2026-3721). A permanently red nightly hides the next real advisory behind it. `pip` is bumped to 26.2.1 in the lockfile.

## Watchdog no longer restarts a run that already finished

A clean quiescence self-stop journals `run_completed` and exits, but the recovery watchdog's stand-down check only recognised teardown in progress, so it restarted the orchestrator anyway — five times, until it gave up and logged a spurious error. The check now reads the run's own completion record before restarting it (#4445).
