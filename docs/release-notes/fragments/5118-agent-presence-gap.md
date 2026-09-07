## Which governed targets lack the agent is now a query

Nothing answered "which of the targets we know about do not have our
agent" — there was no probe recording presence and no attribute for a
selector to ask through.
`bernstein.core.govern.agent_presence.AgentPresence` records one probe's
result, `apply_presence` writes it onto an inventory node as ordinary
attributes, and `enrollment_gap(store)` is the gap list. Because presence
is ordinary attributes, the list is a selector an operator can write
themselves and compose with `region`, group membership or anything else —
and it stays live rather than going stale the way a maintained sheet
does.

A target the probe reached and found unable to host the agent is
`unenrollable` **with the reason recorded**, not merely excluded, so
"cannot ever be enrolled here" stays distinguishable from "not yet". A
target no probe has run against carries no `enrollment` attribute and is
not reported as a gap: "we have not looked" is not "the agent is
missing".

`unenrollable` here is a probe-observed fact. An operator-declared
exclusion is a different thing — a revocable decision needing an author
and a date — and belongs on its own key rather than collapsing both
meanings into one attribute (#5118).
