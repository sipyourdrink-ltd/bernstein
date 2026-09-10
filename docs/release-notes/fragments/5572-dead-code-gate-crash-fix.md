## Fixed a crash in the dead-code quality gate

`GateRunner._run_dead_code_gate_sync` called `self._build_dead_code_result`,
a method `GateRunner` never defines or inherits. `GateRunnerCommandsMixin`
(`core/quality/gate_commands.py`) defines it, but nothing composes that
mixin into `GateRunner` — confirmed empirically: `GateRunner.__mro__` is
just `(GateRunner, object)`. The gate is disabled by default
(`dead_code_check=False`), which is why this went unnoticed: any operator
who enabled it would have hit `AttributeError` on the very first run.
`_build_dead_code_result` is a `@staticmethod` with no dependency on
instance state, so the fix qualifies the call directly
(`GateRunnerCommandsMixin._build_dead_code_result(...)`) — the same
pattern the file already uses for two other cross-mixin static calls
(`_check_alembic_migrations`, `_check_sql_migrations`). No change to what
the gate reports; it now reports something instead of crashing (#5572).
