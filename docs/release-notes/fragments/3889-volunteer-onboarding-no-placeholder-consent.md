## `bernstein volunteer` no longer signs consent to a placeholder policy

Bare `bernstein volunteer` asked for consent and then wrote a DSSE-signed
consent receipt to `.sdd/runtime/volunteer/consent.json` whose manifest and
sandbox-profile digests were invented strings (`placeholder-no-project-selected-yet`,
`hardened-sandbox-profile`), signed by a key generated for that one run. A
consent receipt exists to bind the donor's key to the exact policy a task runs
under, so a verifier checking only the signature would accept it as consent
to a policy the donor never saw. Its closing lines also named prerequisites
(#3885, #3887) as unmerged after both had landed.

Onboarding now explains the flow, records nothing, and exits non-zero naming
what is actually missing: no task source exists yet to pick a project, so there
is nothing for a consent receipt to bind - the same gap
`bernstein volunteer autopilot` refuses on. The onboarding tests run in a
scratch directory, so the test suite no longer writes a receipt into the
checkout's own `.sdd/` (#3889).
