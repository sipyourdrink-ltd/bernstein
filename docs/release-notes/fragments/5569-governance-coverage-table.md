## A table mapping each governance control to the failure it prevents

`docs/governance/coverage.md` lists the failure classes an agentic workload
exhibits, the control that answers each one, where it is enforced, and the test
that fails when the control is removed.

It is generated from `docs/governance/coverage.yaml` and checked in CI, because
the column that rots is the citation: a row naming a test that was renamed or
deleted still reads as an assurance.

The rows worth reading first are the ones that do not say `covered`. Each states
its residual risk in plain language and links the issue that closes the gap, and
a row may not claim coverage without a test.
