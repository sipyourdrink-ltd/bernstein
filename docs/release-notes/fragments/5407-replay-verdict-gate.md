## Gate proposal application on an explicit replay verdict

`_apply_proposal` now refuses to apply an evolution proposal unless its
`replay_verdict` has been set to `ReplayVerdict.ACCEPT` by the replay step.
A proposal whose verdict is `None`, `ReplayVerdict.INVARIANT_VIOLATED`, or
`ReplayVerdict.CHANGED_UNEXPECTEDLY` is gated: the executor is not invoked,
the circuit breaker is not updated, and the proposal logs at INFO with
`Proposal <id> gated: replay_verdict=<verdict>`. The fast-track bypass
that produced a synthetic `passed=True` sandbox result for low-risk
proposals is gone — `run_cycle`, `_shared.make_fast_track_sandbox_result`,
`loop._make_fast_track_sandbox_result`, and the `risk_route == fast_track`
branch have been removed, so every proposal goes through the same replay
path before it can be applied. The concrete outcome is that a proposal
that has not been replayed cannot be applied: the acceptance test
`tests/unit/test_evolution_loop.py::test_run_cycle_gated_on_missing_replay_verdict`
exercises a proposal with no replay verdict and asserts the gate fires.

(Closes #5407)
