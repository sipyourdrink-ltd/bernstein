# Decision records

Each file here records one decision that shaped the system: what the
problem was, what was chosen, and what was given up for it. A record is
written when the decision is made and then left alone. One that gets
edited to match today's code stops being evidence of anything.

Read one when you want to know *why* something is the way it is. The
answer is often "the obvious alternative was tried and it lost", and
that is the part a record preserves and a commit message does not.

| ADR | Decision | Status | Date |
|---|---|---|---|
| [001](001-agent-lifecycle.md) | Agent lifecycle model: agents that exhaust their queue exit rather than idle | Accepted | 2026-03-22 |
| [002](002-withdrawn.md) | Withdrawn — the number is recorded, not reused | Withdrawn | 2026-05-10 |
| [003](003-self-evolution.md) | Self-evolution feedback loop architecture | Approved | 2026-03-22 |
| [004](004-file-based-state.md) | Persistent state is plain text under `.sdd/` — no embedded database | Accepted | 2026-03-22 |
| [005](005-short-lived-agents.md) | Agents spawn with 1–3 tasks, execute, and exit | Accepted | 2026-03-22 |
| [006](006-no-embedded-llm.md) | Scheduling and lifecycle are deterministic Python; zero tokens on coordination | Superseded by [011](011-model-drafts-human-signs.md) | 2026-03-22 |
| [007](007-pluggy-plugin-system.md) | Pluggy is the plugin surface, so extending does not mean forking | Accepted | 2026-03-22 |
| [008](008-click-for-cli.md) | Click for the CLI | Accepted | 2026-03-22 |
| [009](009-lineage-v1.md) | Lineage v1: per-artefact transparency log | Accepted | 2026-05-13 |
| [010](010-audit-chain-default-cost.md) | What turning the HMAC audit chain on by default costs, and the path to it | Accepted | 2026-07-24 |
| [011](011-model-drafts-human-signs.md) | A model may draft an artefact a human must sign; it never decides what runs | Accepted | 2026-09-02 |

## When a record is required

A record is required when a decision closes off an alternative someone
will reasonably propose again later. That is the test — not how large
the change is. A small decision that will be re-litigated every quarter
is worth a record; a large one nobody will question is not.

Concretely, write one before the change merges when any of these is
true:

- it narrows or widens a boundary listed in [Scope](../scope.md);
- it makes a claim the project states publicly, or makes an existing
  one narrower;
- it picks one of several defensible designs, and the losing one is
  something a reviewer would otherwise ask for by default;
- it changes what a stored artefact means to a reader six months later.

Skip it when the choice is reversible at no cost, or when the code
already states the reason plainly enough to survive the next reader.

Six months of arguments in issue threads is not a record. A thread
holds the argument; a record holds which way it went and what it cost.
When the two diverge, the record is what binds.

## Superseding, withdrawing, numbering

A record is never rewritten to match what the code does now. When a
decision changes, write a new record and set the old one's status to
`Superseded by ADR-NNN`, leaving its body as written. Both stay
readable, so a reader can see the position that was replaced and why.

Numbers are addresses. A record that is withdrawn keeps its number and
a file stating so; a number is never reused, and the sequence never has
a gap that the directory itself cannot explain.

## Format

Follow the existing files: number in sequence, `Status`, `Date`,
`Context`, then the problem, the decision, and what it costs. State the
alternatives that lost and why — a record with no rejected alternative
is a description, not a decision. Cite the issues the reasoning came
from, so a reader can get to the argument behind the outcome.

`tests/unit/test_decision_records.py` checks the parts of this that a
machine can check: every record carries a status and an ISO date, the
numbering has no unexplained gap, a superseded record names a successor
that exists, and the table above matches the directory.

The boundaries these records set are collected in
[Scope](../scope.md), which is the page to link when a proposal runs
into one of them.
