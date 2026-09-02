# Unreleased

Changes merged to `main` that are not yet part of a tagged release. Each
tagged release has its own page in this directory; this page carries what has
landed since the newest one.

Cutting a version empties this page: every entry the tag ships moves onto that
version's page in the release PR itself. `tests/unit/test_unreleased_notes_rotation.py`
holds the page to that — an entry naming an issue or PR a tagged release page
already documents fails the build. An entry that cites released work as context
rather than as its own attribution is exempted by hand there, with the reason.


## Lineage

- Lineage entries carry an additive optional `sensitivity` classification, and the effective sensitivity of an artefact is the maximum class over its lineage closure — so a summary of a confidential document is confidential. Absence fails closed to the highest class, and the verdict names the closure member that raised the level and the path through the graph that reaches it. An entry without the field canonicalises byte-identically to the pre-change schema, so every historical signature and HMAC is untouched (#5042).
