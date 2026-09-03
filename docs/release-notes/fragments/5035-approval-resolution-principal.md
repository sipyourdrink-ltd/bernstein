## Every approval resolution names the principal that made it

An approval resolution now carries a structured principal — an identifier, the
method it was authenticated by, and the session or grant it acted under — and
there is no default, so a surface that cannot name the decider cannot resolve.
The principal is written to the resolution sentinel under
`.sdd/runtime/approvals` and to the `human_approval_decision` audit-chain
event, whose actor is now the principal's identifier rather than the channel
the decision arrived on. Decisions the software makes on its own (TTL expiry,
the eviction sweeper, a policy deny, the auto-approve classifier) are recorded
under the reserved `system:` namespace, which a human principal may not use, so
an unattended rejection cannot be read as human oversight. The card gate no
longer substitutes `operator` for a blank approver: it refuses, chains the
refusal, and leaves the card decidable. Over HTTP, a request that presents no
valid scoped token while scoped tokens are configured is answered with `401`
instead of being attributed to an unnamed caller. (#5035)
