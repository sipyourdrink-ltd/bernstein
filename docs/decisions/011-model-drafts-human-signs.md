# ADR-011: A Model May Draft What a Human Signs

**Status**: Accepted
**Date**: 2026-09-02
**Supersedes**: [ADR-006](006-no-embedded-llm.md)
**Context**: `src/bernstein/core/govern/` (`findings.py`, `proposal.py`),
issues #4973, #4981, #4982, #5020

---

## Problem

ADR-006 described the system as having two places a model can be. Coordination
— scheduling, task assignment, lifecycle, retry — is deterministic Python and
spends zero tokens. Everything else a model does happens at "the explicit leaf
nodes of the system": decomposing a goal into tasks, reviewing a finished diff,
verifying a completed change.

`govern discover --assist` (#5020) is in neither place. It hands a findings
document — built entirely from chain-recorded observations — to a model the
operator nominates, and gets back a draft governance playbook. The scheduler
never sees it. No task is executing while it runs. Its output governs future
runs rather than performing one.

A contributor reading ADR-006 before writing that command has two bad options.
They can decide it is out of bounds and not write it, which costs a feature the
record never actually forbade. Or they can call it a leaf node, which quietly
stretches "leaf" to mean "wherever a model turned out to be fine" — and a
boundary that stretches on contact is not a boundary.

The claim itself is also load-bearing outside the repository. We state publicly
that no model sits in the coordination loop. That statement is still exactly
true and needs to stay checkable, which it cannot be while the record it rests
on does not describe what ships.

---

## Decision

There are three categories, not two.

1. **Coordination.** Deterministic Python. No model, no tokens. Unchanged from
   ADR-006.
2. **Leaf execution.** A model performs a task inside a run — decomposition,
   review, cross-model verification. Unchanged from ADR-006.
3. **Drafting.** A model produces an artefact that has no effect until a human
   signs it.

The rule a contributor can apply without asking:

> **A model may draft an artefact a human must sign. It may never decide what
> runs next, which agent gets it, whether it is retried, or when it ends.**

"Must sign" is a property of the artefact, not a convention. A drafted artefact
is recorded as a proposal, and the command that would act on it refuses while it
is unsigned: `DraftProposal.is_signed()` in
`src/bernstein/core/govern/proposal.py` is what `govern apply` (#4982) checks,
and an unsigned draft is a refusal, not a warning.

Three things are recorded with the draft, so it can be read later as evidence
rather than as a suggestion: which model produced it, the digest of the prompt
it was given, and the digest of the findings document it read. The findings
document is content-addressed and on the chain, so "what did it actually see"
is answerable six weeks later without re-running anything (#5020).

---

## Why the coordination claim survives

The drafting call is not on the tick loop and no run's task graph depends on it.
A run consumes a playbook that a human already signed; replaying that run
replays against the same playbook by hash. Two operators with the same signed
playbook get the same task graph, which is the property ADR-006 existed to
protect and the one that would break if a model sat between "a task finished"
and "what runs next".

What a drafting model changes is what a human was shown before they decided.
That is worth recording, which is why it is, and it is not the same thing as
changing what the scheduler did.

---

## Rejected alternative: leave ADR-006 alone and call drafting a leaf node

Cheapest option, and it is the one a contributor under time pressure will reach
for. Rejected because a leaf node executes a task inside a run and its output is
consumed by that run; a drafting command runs outside any run and its output
governs later ones. Folding the second into the first leaves "leaf node" meaning
nothing in particular, and the next contributor stretches it one step further —
this time toward something that does touch scheduling.

## Rejected alternative: edit ADR-006 in place

`index.md` states the reason this directory works: a record is written when the
decision is made and then left alone, because one edited to match today's code
stops being evidence of anything. ADR-006 was not wrong about what it decided.
It is incomplete about a case that did not exist when it was written. Editing it
would erase the fact that the two-category model was ever the position, which is
the part a reader needs in order to trust the third category was argued for
rather than assumed.

Ten accepted records and zero supersessions across a moving architecture is a
record being appended to. This is the first supersession, and the mechanism it
establishes — status changes to `Superseded by ADR-NNN`, body untouched — is
how the next one goes.

## Rejected alternative: forbid model drafting outright

Defensible on the surface: the narrowest rule is the easiest to police.

Rejected because it does not remove the model, it removes our record of it. The
reason an environment has no governance playbook is almost always that nobody
can enumerate the environment (#5020). An operator facing that will ask a model
anyway, in a chat window, with no digest of what it read and no signature on
what came out. Forbidding the supported path trades an attributable draft for an
unattributable one and calls the result stricter.

The fallback stays first-class rather than degraded: with no connector
configured, the command still emits the findings document and no draft.

---

## Consequences

### Benefits

**A legitimate feature has an answer.** #4981 and #5020 stop depending on how a
reviewer reads "leaf node".

**The public claim gets narrower and true.** "No model in the coordination loop"
is a statement about scheduling, and it stays exactly true. It was never the
same claim as "no model anywhere but leaf execution", and only the second one
would have had to be withdrawn.

**Supersession is now a thing this directory does.** The status flip on ADR-006
is the worked example.

### Costs

**A third category is more surface to police.** Two categories could be checked
by reading an import; three cannot. The check that replaces it is the signature
requirement: a command that writes a governance artefact from model output must
produce something `apply` refuses until a human signs it. A drafting path that
skips that is not in category 3 — it is an unrecorded change to a security
posture.

**Category 3 will be argued about at its edge.** "The human signs it" is doing
the work, so the pressure lands on what counts as signing. A signature is a
human action on a specific content hash. A default-on setting that auto-accepts
drafts is not one, whatever it is called.

---

## What is still forbidden

Unchanged from ADR-006, and not softened by any of the above. No model decides:

- which task runs next, or in what order;
- which agent a task is assigned to;
- whether a failed task is retried or abandoned;
- when an agent's life ends.

Those are deterministic Python, and a proposal to move any of them is answered
by [Scope](../scope.md).
