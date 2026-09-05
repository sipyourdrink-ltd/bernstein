## Conformance vectors for the authority plane

Four auditor questions now have vectors — 3 (was the sub-agent authorized, and
by whom), 4 (what exactly was it permitted to do), 5 (did it stay inside that
permission) and 21 (which other principals hold authority derived from the same
grant).

All four fail, as `xfail(strict=True)`, and that is the deliverable. The bundle
carries no grant, capability, scope or delegation receipt of any kind, so the
permitted set is unreadable even in principle and containment has nothing to be
compared against. Each vector names the field that is missing and the issue
that would add it, and `strict` means the day one lands the build fails until
the vector is un-marked.

Question 3 is partly answerable, and the split is recorded rather than glossed:
the bundle *does* say who started the sub-agent — `agent_spawned.started_by` in
the journal and `agent.delegated` in the audit chain both name it — and never
that the parent held any authority to delegate. The question-marked vector
carries the `xfail`; a separate unmarked test pins the attribution half so a
regression there goes red instead of disappearing into it.

The auditor scoreboard now asks 10 of 21 questions, up from 6 (#5059).
