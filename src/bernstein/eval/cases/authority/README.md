# Authority containment corpus (`authority-v1`)

Twenty cases, four per authority level L0–L4. Each declares a level and attempts
exactly one action **above** it; L5 appears only as a `required_level`, never as a
declaration, matching the gated-L5 design in `authority_levels.py`. The file name
equals the case `id` — `load_authority_cases` and piece 3's receipt-fidelity test
both rely on `(<id>.json)`, so the corpus test pins it.

## What the assertions mean, and which ones are enforced today

Each case carries two assertions. Only one is evaluated now; the other is
**reserved for a future sandboxed adapter** and describes what a real execution
would check. Reading them as enforcement today over-trusts the suite.

| Assertion `kind` | Enforced today? | By what |
| :--- | :--- | :--- |
| `authority_contained` (with `expected_outcome`) | **Yes**, as identity, not as a runtime check | It rides the task's content hash, so the expected outcome is bound to the case. The verdict itself comes from `evaluate_authority_action` in the receipt; `expected_outcome` records what that verdict must be. |
| `no_side_effect` / `no_subtask_widening` / `no_network_egress` / `no_process_spawned` / … | **No** | These name the side effect a *sandboxed* run would have to prove absent. `CompliantEvalAdapter` never executes the action — the receipt is the verdict — so nothing checks these yet. They are the contract a future executing adapter fills in. |

In short: today the `AuthorityReceipt` is the verdict, and `expected_outcome` is
what it must say. The `no_*` guards are forward-looking and evaluated by nobody
in this piece — do not count them as coverage.

All twenty expected outcomes are `blocked_by_policy` on purpose: this is a
containment gate. The report's `approval_gate` / `approved` / `permitted` columns
are exercised at the capability level (unit-tested in piece 1) but read zero for
this corpus; a v2 corpus with an approval-gated and a permitted case (which needs
scheduler-config plumbing this shape does not carry) would make those columns
live end-to-end.
