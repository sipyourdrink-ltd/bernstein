## Proposed isolation of mutation-timeout unit probes

This draft proposes moving mutation-timeout unit probes into a temporary,
test-owned source tree. The current probes invoke the real mutation harness
against the shared checkout while mocking subprocess outcomes. A concurrent
test importer can therefore observe a truncated or mutated production module,
even when the probe passes and later restores the original bytes. The proposed
fixture will isolate the probe's `REPO` and source without changing production
mutation behavior, timeout budgets, or scoring. A deterministic concurrent-import
regression and the existing timeout controls will verify the correction before
this proposal is described as implemented. This is a partial contribution to
#6471; it will not establish a green mutation nightly on its own.
