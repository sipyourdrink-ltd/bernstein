## Accurate GateRunnerCommandsMixin docstring on composition and initialization

`GateRunnerCommandsMixin`'s docstring previously claimed that it was combined
with `GateRunner` at runtime and that `__init_commands__` was called from
`GateRunner.__init__`. In reality, `GateRunner` does not inherit or compose the
mixin, and static helpers (such as `_build_dead_code_result`) are referenced
directly by qualified name. The docstrings and comments now accurately document
this relationship without claiming nonexistent runtime composition (#5682).
