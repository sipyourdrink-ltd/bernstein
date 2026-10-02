## Review roster gains triagers, area reviewers and role terms

`.github/quorum-roster.toml` now has `triagers` (no approval weight) and
`area_reviewers` (core only inside their area: adapters, web, tui, docs,
packaging), and any entry may carry an `expires` date or a `granted` date
with a six-month term; an expired entry counts as absent. Existing rosters
load unchanged. The charter's new "Roles and terms" section and an
"Election" paragraph in GOVERNANCE.md describe the ladder; the roster stays
maintainer-only. `scripts/roster_terms.py` warns about entries ending within
30 days.
