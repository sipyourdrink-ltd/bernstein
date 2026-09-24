# Unreleased

Changes merged to `main` that are not yet part of a tagged release. Each
tagged release has its own page in this directory; this page carries what has
landed since the newest one.

Cutting a version empties this page: every entry the tag ships moves onto that
version's page in the release PR itself. `tests/unit/test_unreleased_notes_rotation.py`
holds the page to that — an entry naming an issue or PR a tagged release page
already documents fails the build. An entry that cites released work as context
rather than as its own attribution is exempted by hand there, with the reason.

## Sandbox

- A concurrent reclaim landing between `Lease.keepalive()`'s read and write could silently overwrite a lease another claimant had already taken, and the same gap existed on the reclaim side of `LeaseStore._open_exclusive`. Both now share one per-resource `flock` (the same primitive `chain._exclusive_lock` already uses, including its Windows no-op fallback), so whichever side wins finishes its full read-check-write before the other can act (#5908).
