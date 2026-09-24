## DENY over IMMUNE is pinned by a test, not only by a comment

`DecisionGraph.evaluate()` ranks pending decisions through a precedence table
and returns the first survivor. No test put a `DENY` and an `IMMUNE` decision
in the same graph, so giving the two the same rank stayed green: `sorted` is
stable, and the winner -- with it the reason the operator reads and the
`bypass_immune` flag consulted -- would have quietly become insertion-ordered.
Both orders are now asserted, with and without bypass enabled (#5948).
