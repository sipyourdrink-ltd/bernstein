# Unreleased

Changes merged to `main` that are not yet part of a tagged release. Each
tagged release has its own page in this directory; this page carries what has
landed since the newest one.

Cutting a version empties this page: every entry the tag ships moves onto that
version's page in the release PR itself. `tests/unit/test_unreleased_notes_rotation.py`
holds the page to that — an entry naming an issue or PR a tagged release page
already documents fails the build. An entry that cites released work as context
rather than as its own attribution is exempted by hand there, with the reason.

- `bernstein audit export --standard` gained `iso-42001`, mapping the
  records-derivable subset of ISO/IEC 42001 Annex A controls (event logging,
  operation monitoring, data provenance, third-party and data resources,
  human oversight) onto the same audit chain, lineage log and cost ledger the
  `ai-act`, `owasp-asi` and `owasp-skills` packs already read. Every control
  resolves to `mapped`, `partial`, or `organisational` (named explicitly when
  no chain record can evidence it), and the pack's control counters now count
  `organisational` controls instead of dropping them from the summary. See
  `docs/compliance/iso42001-mapping.md`. (#3238)

