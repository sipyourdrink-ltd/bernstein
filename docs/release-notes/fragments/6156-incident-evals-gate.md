## `incident_evals` removed from the recognised gate names

`incident_evals` was accepted by pipeline configuration (it was listed in
`VALID_GATE_NAMES`) but had no entry in `GateRunner`'s dispatch tables, so
any pipeline naming it failed at run time with `ValueError: Unsupported
gate name: 'incident_evals'` instead of at configuration validation.

Rather than standing up a real gate for it, `incident_evals` is removed
from `VALID_GATE_NAMES`: nothing in the default pipeline, the
gate-evasion corpus, or any other consumer names it today, so there is no
current caller for a dispatchable version to serve. `run_incident_eval_gate()`
in `src/bernstein/eval/incident_synthesizer.py` - the function a gate would
have called - is untouched and still available as a library function for a
future gate that does have a real consumer (#6156).
