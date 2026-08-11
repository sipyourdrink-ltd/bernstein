# Roadmap

What is being built, in what order, and what each milestone commits to.

The section below is projected from the open GitHub milestones. The
milestone is where the work actually lives; this file is a view of it
rather than a second copy that can quietly disagree. Issue counts stay
live on the milestone pages linked here, deliberately — a roadmap that
changes every time an issue closes is a roadmap nobody reads twice.

<!-- roadmap:generated:start -->

### [v3.15.0](https://github.com/sipyourdrink-ltd/bernstein/milestone/19) — due 24 August 2026

First-run experience and adapter coverage: the defects a new user hits in the first hour, plus the adapter additions already in review.

### [v3.16.0](https://github.com/sipyourdrink-ltd/bernstein/milestone/20) — due 21 September 2026

Verifiability and reliability deepening: receipt coverage, lineage surfaces, scheduler correctness. No breaking changes.

### [v4.0.0](https://github.com/sipyourdrink-ltd/bernstein/milestone/10) — scoped by content, no date

Architectural and breaking work: zero-code adapter onboarding, CLI surface consolidation, web UI maturation. Scoped by content, not by date.

<!-- roadmap:generated:end -->

## What a milestone means here

A milestone **with a date** commits to the date, not to the contents.
It ships on the date with whatever met the bar; anything that did not
moves to the next one. That way a slip is visible as a moved issue
rather than as a quiet delay.

A milestone **without a date** is scoped by content instead. It ships
when the content is done. Putting a date on it would invent a
commitment nobody made, which is worse than admitting the work is not
schedulable yet.

## Where to plug in

- [Good first issues](https://github.com/sipyourdrink-ltd/bernstein/issues?q=is%3Aopen+is%3Aissue+label%3A%22good+first+issue%22)
  — self-contained, with acceptance criteria written out, no prior
  context required.
- [Help wanted](https://github.com/sipyourdrink-ltd/bernstein/issues?q=is%3Aopen+is%3Aissue+label%3A%22help+wanted%22)
  — larger, still scoped.
- [Adapters](https://github.com/sipyourdrink-ltd/bernstein/issues?q=is%3Aopen+is%3Aissue+label%3Aadapter)
  — support for another coding CLI. The most self-contained kind of
  change in the repo: one module, one conformance test, one doc entry.

Every issue in those queries states what "done" looks like before you
start. If one does not, that is a defect in the issue — say so on it.

Comment on an issue to claim it. If it is already assigned and quiet
for a couple of weeks, ask anyway; stalled is not the same as taken.

## What is not planned

[Scope](docs/scope.md) lists the boundaries that are already decided
and why. It is the page to read before proposing something large, and
the page to argue with if you disagree — the reasons are stated so they
can be attacked. The decisions behind them are in
[decision records](docs/decisions/index.md).

## How this file stays true

`scripts/gen_roadmap.py` regenerates the block between the markers from
the milestones API. A scheduled job runs it and opens a pull request
when the projection changed. The render is deterministic and carries no
timestamp, so an unchanged roadmap produces no pull request at all.

Editing the generated block by hand is pointless — the next refresh
overwrites it. Edit the milestone.
