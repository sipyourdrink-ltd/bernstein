## Keep mutation-timeout unit probes out of the shared checkout

Mutation-timeout unit probes now copy their source into a per-test temporary
repository before invoking the real mutator. Concurrent test processes can no
longer import a truncated or mutated production module from those probes. A
deterministic regression pauses each real probe during its write while a
separate notifications test process imports production code. Timeout budgets,
scoring, and the production mutator remain unchanged. This is a partial
contribution to #6471; it does not establish a green mutation nightly on its own.
