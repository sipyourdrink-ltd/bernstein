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
| [003](003-self-evolution.md) | Self-evolution feedback loop architecture | Approved | 2026-03-22 |
| [004](004-file-based-state.md) | Persistent state is plain text under `.sdd/` — no embedded database | Accepted | 2026-03-22 |
| [005](005-short-lived-agents.md) | Agents spawn with 1–3 tasks, execute, and exit | Accepted | 2026-03-22 |
| [006](006-no-embedded-llm.md) | Scheduling and lifecycle are deterministic Python; zero tokens on coordination | Accepted | 2026-03-22 |
| [007](007-pluggy-plugin-system.md) | Pluggy is the plugin surface, so extending does not mean forking | Accepted | 2026-03-22 |
| [008](008-click-for-cli.md) | Click for the CLI | Accepted | 2026-03-22 |
| [009](009-lineage-v1.md) | Lineage v1: per-artefact transparency log | Accepted | 2026-05-13 |
| [010](010-audit-chain-default-cost.md) | What turning the HMAC audit chain on by default costs, and the path to it | Accepted | 2026-07-24 |

## When to write one

Write a record when a decision closes off an alternative someone will
reasonably propose again later. That is the test — not how large the
change is. A small decision that will be re-litigated every quarter is
worth a record; a large one nobody will question is not.

Skip it when the choice is reversible at no cost, or when the code
already states the reason plainly enough to survive the next reader.

## Format

Follow the existing files: number in sequence, `Status`, `Date`,
`Context`, then the problem, the decision, and what it costs. State the
alternatives that lost and why — a record with no rejected alternative
is a description, not a decision.

The boundaries these records set are collected in
[Scope](../scope.md), which is the page to link when a proposal runs
into one of them.
