# Docs source-of-truth notes

This page records which documentation surfaces are treated as
source-of-truth and the editing rules contributors should follow when
touching them. It is meta-documentation; reading it is not required to
use Bernstein. Skip to [README.md](https://github.com/sipyourdrink-ltd/bernstein/blob/main/README.md) for the project itself.

## Source-of-truth surfaces

| Surface | Path | Why it is canonical |
|---|---|---|
| README "at a glance" | [README.md](https://github.com/sipyourdrink-ltd/bernstein/blob/main/README.md) | First paragraph downstream tools scrape into AGENTS.md overview. Numbered facts (adapter count, RFC list) with explicit source pointers. |
| Regulatory anchors | [docs/reference/capabilities.md](reference/capabilities.md) | Maps each compliance claim to an RFC or framework name; relocated from the README front page. |
| HMAC audit operator guide | [docs/security/audit-log.md](security/audit-log.md) | RFC 2104 anchor, key-rotation runbook, exact JSONL schema. |
| Lethal-trifecta security model | [docs/security/lethal-trifecta.md](security/lethal-trifecta.md) | Capability-matrix table plus primary-source quote. |
| Lineage export guide | [docs/compliance/lineage-export.md](compliance/lineage-export.md) | RFC 8037 Ed25519 anchor, schema version, walkthrough. |
| `bernstein audit --help` | `src/bernstein/cli/commands/audit_cmd.py` | Per-subcommand docstrings cite RFC 2104 / 7515 / 8785. |
| `bernstein lineage --help` | `src/bernstein/cli/commands/lineage_cmd.py` | Per-subcommand docstrings cite RFC 8037 / EdDSA. |
| `bernstein agents-md --help` | `src/bernstein/cli/commands/agents_md_cmd.py` | Cites canonical [agents.md/](https://agents.md/) spec and AAIF. |

## Editing rules

These are non-negotiable. If your change fails any of them, revert and
re-cut.

1. **No invented stats.** Every numeric claim ("40+ adapters", "296 stars")
   must trace to a checked source: a count computed from the codebase at
   PR time, the GitHub API at PR time, or a primary-source paper. State
   the date inline so the reader can refresh.
2. **No fabricated quotations.** A quote must be verbatim, attributed, and
   linkable. If you cannot find the URL, the quote does not go in.
3. **RFC pins are exact.** "RFC 2104" not "the HMAC RFC". Link to
   `datatracker.ietf.org/doc/html/rfcNNNN`.
4. **Distinguish prod-tested from spec-only.** "Z3/Lean4 formal property
   checks" is `bernstein verify --formal`: the surface ships, the backends
   are gated. Say so. Do not claim certifications Bernstein does not
   have (no SOC 2 Type II, no ISO 27001).
5. **Keep voice.** Lowercase headings and casual prose are part of the
   project's identity. Avoid "comprehensive", "robust", "delve", or
   em-dash glue. If your edit reads like a landing page, recut.
6. **Date number claims.** "as of YYYY-MM-DD: ..." on every count that can
   drift. The date is the freshness signal; without it the number rots.
7. **Prefer primary sources.** Anthropic / OpenAI / FINOS / IETF over
   secondary write-ups. Link the paper, not the blog post about the paper.

## Refresh cadence

- README "at a glance" stats: quarterly, or on any minor release that
  ships an adapter or regulation surface.
- RFC list: only when an RFC is added or replaced (rare).
- This page: annually, plus when the editing rules above need to change.
