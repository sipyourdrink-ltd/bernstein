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
- An operator-controlled source-to-sensitivity map (`templates/provenance/sensitivity_sources.yaml`, overridable per project) says which class a source's results carry; an unlisted source fails closed to the highest class and an unrecognised class token is dropped rather than coerced (#5042).
- `bernstein lineage sensitivity <artefact|entry-hash>` reports the effective class, the closure member that raised it and the walk through the graph that reaches it, with `--json` for scripts. The lineage gate runs first: a failing log exits 1 without printing a class, and an unknown artefact or a missing log reports the fail-closed class (#5042).
