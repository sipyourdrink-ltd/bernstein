## Evolution no longer halts itself on its first proposal

`CircuitBreaker.record_rollback` trips on any rollback inside 48 hours, and the
timestamp it checks is the one it just appended — so the first rollback always
trips. That is the intended policy for a system that edits itself.

What it was being fed was the problem. `execute_upgrade` answers `False` for
three different situations — admission refused the proposal, the category had
no sink, or an apply failed partway — and only the last undid anything. Every
category resolves to a no-sink skip today, so the loop's failure path ran for a
proposal that never touched the tree, recorded it as a rollback, and halted
evolution permanently on the first proposal it ever saw, with a reason reading
"Rollback detected" for a tree nothing had changed.

The executor now answers `was_applied(proposal_id)` from the history it already
writes, and the loop records a rollback only when something was actually
reverted. Asked before the rollback, which writes `rolled_back` to history and
would flip the answer.

A second halt rule in the same method — `elif len(recent_rollbacks) > 2`,
reading ">2 rollbacks in 7 days" — is removed. It could never run: the append
guarantees the 48-hour list is non-empty whenever the list is, so the branch was
unreachable for every possible history. A rule stated in code that cannot fire
reads as a policy the system has and does not.
