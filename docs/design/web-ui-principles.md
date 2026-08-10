# Web UI — design principles

The browser operator UI has eight screens, a task drawer with six feature
panels, and no written description of what it is trying to be. Contributors
are adding screens against it (tracking issue #1262), and the vocabulary they
match is whatever screen they happened to read first. This file is that
description. It is short on purpose.

Scope: `web/` only. The TUI is a separate surface with its own constraints.

## What the UI is

A read-mostly view over records the orchestrator already produced. The run
happened elsewhere; the dashboard reports it. Almost every defect this surface
can have is a place where the view says something the records do not.

## Principles

Each has an identifier so an issue or a review comment can cite one.

**P1 — Every figure resolves to a record.** A number on screen traces to a
specific entry the operator can open. Aggregates state what they aggregate
over. A figure the browser computed from data it does not display is not
verifiable by the person reading it.

**P2 — Absence renders as absence.** When there is no data, the screen says so
in the shape of the thing that is missing ("no approvals recorded for this
run") rather than a zero, a dash, or a skeleton that never resolves. A
plausible-looking placeholder is worse than an empty state, because it reads
as a fact.

**P3 — Same records, same view.** Two operators opening the same run see the
same content. Rendering that depends on the reader — relative timestamps as
the only form, locale-ordered lists, values that drift with the current clock —
gets an absolute form alongside it.

**P4 — Verification is reachable from where the claim is shown.** A screen
displaying attested data shows the command that re-checks it, next to the
claim rather than in a documentation page. The operator should never have to
find out elsewhere how to confirm what they are looking at.

**P5 — Quiet by default, dense when asked.** Operators keep this open for
hours. The type scale already encodes this: 13px body, 11px metadata,
uppercase tracking reserved for labels. Colour carries emphasis sparingly;
motion is short (90ms fades, 250ms drawer) and never loops.

**P6 — State is never carried by colour alone.** Every `success` / `warning` /
`destructive` token appears with a text or icon form of the same information.

**P7 — The orchestrator is the only host the browser talks to.** The
dashboard fetches records from the API and event stream it was pointed at
(`web/src/lib/api.ts`, `web/src/lib/sse.ts`) and from nowhere else. Fonts are
vendored; there are no third-party asset hosts, no analytics, no telemetry.
The project ships an air-gap profile, and an operator opening a dashboard
should not announce that outside their own deployment.

**P8 — Keyboard reaches everything the mouse reaches.** The command palette is
the primary route; a control that exists only as a click target is incomplete.

## What this UI will not do

Stated so a proposal can be declined by pointing at a line rather than a
preference.

- No usage analytics or telemetry from the operator dashboard.
- No third-party fonts, scripts, or images loaded at view time.
- No status the source records do not contain, however reasonable the guess.
- No blocking modal for an action that is not destructive.
- No infinite spinner: a load either resolves, fails visibly, or is cancelled.
- No screen whose only content is a chart. Charts sit beside the rows they
  summarise, so the reader can check one against the other.

## Screens today

| Route | File | Reports |
|---|---|---|
| Tasks | `web/src/routes/Tasks.tsx` | task list and the per-task drawer |
| Approvals | `web/src/routes/Approvals.tsx` | pending and decided approvals |
| Audit | `web/src/routes/Audit.tsx` | audit chain entries |
| Costs | `web/src/routes/Costs.tsx` | spend by task, agent, model |
| Fleet | `web/src/routes/Fleet.tsx` | workers and their liveness |
| Agents | `web/src/routes/Agents.tsx` | configured adapters |
| Missions | `web/src/routes/Missions.tsx` | mission-level grouping |
| Settings | `web/src/routes/Settings.tsx` | local preferences |

Task drawer panels live under `web/src/components/<feature>/`: `artifacts`,
`deps`, `diff`, `gates`, `logs`, `trace`.

## Using this file

A pull request that adds or changes a screen names the principles it is
working under and, where it departs from one, says which and why. A reviewer
who wants a change cites the identifier. If a principle turns out to be wrong,
change it here first — a rule nobody can amend gets worked around instead.

The vocabulary these principles are written against — tokens, type scale,
motion, component families — is catalogued in
[web-ui-inventory.md](web-ui-inventory.md).
