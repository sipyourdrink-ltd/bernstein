## suppress

`bernstein audit suppress ID --reason '...' --until YYYY-MM-DD` records a
bounded-time `GovernanceDecision` (verdict=accepted, action=suppress) anchored
in the govern-audit spine. Suppressed findings appear in audit reports as
accepted with the decision's chain anchor; the suppression lapses after
`--until`. Closes #5078.
