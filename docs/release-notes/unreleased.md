# Unreleased

Changes merged to `main` that are not yet part of a tagged release. Each
tagged release has its own page in this directory; this page carries what has
landed since the newest one.

Cutting a version empties this page: every entry the tag ships moves onto that
version's page in the release PR itself. `tests/unit/test_unreleased_notes_rotation.py`
holds the page to that — an entry naming an issue or PR a tagged release page
already documents fails the build. An entry that cites released work as context
rather than as its own attribution is exempted by hand there, with the reason.


## Governance

- A change receipt now records, per entry, the value the target held
  immediately before the change alongside the value written, and
  `bernstein.core.govern.build_restore_plan` projects an apply receipt into the
  plan that undoes it. Every restore value is read off the receipt; the
  environment is consulted only to refuse an entry whose target drifted since
  the apply or could not be read, which an operator overrides per entry. The
  plan carries the digest of the receipt it inverts, so a restore is tied to
  its apply record without a separate index. The three receipt fields are
  additive and optional, so receipts written before them still verify offline
  and the schema version is unchanged (#5109).
