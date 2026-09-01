---
title: The issue shelf
description: How issues get advertised as available, what each label means, and why an issue loses its advertisement.
---

# The issue shelf

The **shelf** is the set of open issues advertised as available to work on. An issue is
on the shelf when it carries `help wanted` or `up-for-grabs`, plus `good first issue` /
`beginner-friendly` when it is small and has a starting point written into the body.

Those labels used to be applied by hand, which failed in both directions: issues that
were ready sat unadvertised for weeks, and issues somebody had already started kept
their advertisement and sent a second contributor into a collision.
`scripts/issue_shelf.py` decides them on a fixed rule instead.

## What you can rely on as a contributor

| If an issue shows | It means |
|---|---|
| `help wanted` / `up-for-grabs` | Nobody holds it. Comment and it is yours. |
| `good first issue` / `beginner-friendly` | Also small, and the body names a place to start. |
| None of the above, and it is open | Somebody holds it, or it is not ready to hand out yet. |

An advertisement disappearing is not a rejection. It means the issue became held —
usually because someone was assigned, or a pull request now closes it.

## When an issue comes off the shelf

The script removes the bait labels as soon as any of these is true, and it checks them
before it checks anything else:

| Signal | Why it counts as taken |
|---|---|
| An assignee | The explicit record. |
| An open PR whose body says `closes #N` (or fixes / resolves / part of) | Work is already in flight. |
| `reserved`, `blocked`, `needs-better-brief`, `fleet-blocked`, `fleet-running` | A hold was set deliberately. |
| `roadmap` | A tracking issue, not a unit of work. |

`no-bait` is the maintainer override: an issue carrying it is never advertised and never
un-advertised by the script, whatever the other signals say.

## When an issue goes onto the shelf

Only if it is free by every signal above **and** it is genuinely ready to hand to
somebody who has never seen the codebase:

- a size label, and that size is `size/xs`, `size/m` or smaller — anything larger is not
  a single evening, and slicing it further beats describing it better;
- a milestone;
- acceptance criteria in the body;
- a named file, path or symbol to start from.

An issue failing any of these is reported with the specific reason and left alone. The
script never writes a bait label onto an issue whose brief would waste a contributor's
evening.

## The one case where it does nothing

A comment from somebody who is neither a bot nor the maintainer, within the last 14
days, makes the issue **unproven**: the script adds nothing and removes nothing, and
prints it for a human to read. Somebody talking in the thread is evidence, but it is not
evidence of what — "I'll take this" and "does this still happen?" look identical to a
label rule, and guessing wrong costs a contributor either way.

## Running it

Report-only. Changes nothing, needs no write access:

```bash
uv run python scripts/issue_shelf.py
```

Apply the decisions:

```bash
uv run python scripts/issue_shelf.py --apply
```

CI runs the report-only form every six hours and on issue events, and writes the result
to the workflow summary. Applying is a manual dispatch with `apply: true`.

Both forms refuse to start if the repo does not define every label that holds an issue
back. A label the repo has never created cannot be on an issue, so the branch that reads
it never runs — the guard is present in the source and inert in the run, and the shelf
would advertise exactly the work those labels protect.
