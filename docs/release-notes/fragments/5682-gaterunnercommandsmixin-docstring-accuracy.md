## The GateRunnerCommandsMixin composition claim is pinned by a test

`GateRunnerCommandsMixin`'s docstring used to claim it was combined with `GateRunner` at runtime and that `__init_commands__` was called from `GateRunner.__init__`. Neither is true: `GateRunner.__mro__` is `(GateRunner, object)`, and no call site for `__init_commands__` exists. The docstring now says so.

Nothing stopped it drifting back. Two tests pin the claim to the code it describes - one asserting `GateRunnerCommandsMixin` is genuinely absent from `GateRunner.__mro__`, one asserting the docstring does not reassert runtime composition - so a future edit that reintroduces the claim without reintroducing the composition fails rather than misleading the next reader (#5682).
