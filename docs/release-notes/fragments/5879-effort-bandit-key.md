## Effort bandit now learns from the effort level a session actually ran at

`_record_bandit_outcome` fed the routing bandit `getattr(session, "effort", "")`,
which is always empty since the effort level lives on
`session.model_config.effort`. Every outcome was therefore recorded under an
arm the effort bandit does not recognise and silently discarded, so
`EffortBandit` never accumulated pulls and `BanditRouter` always fell back to
the static per-model heuristic instead of the learned effort preference. The
outcome now carries the session's real effort level (#5879).
