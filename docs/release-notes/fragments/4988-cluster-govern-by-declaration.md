## `bernstein cluster govern-inventory` (Issue #4988)

Governance that has to be wired per workload covers the workloads someone
remembered. `bernstein cluster govern-inventory` reads a workload listing and
derives each workload's posture from the `bernstein.io/govern` label it already
carries. Nothing is written back: enrolling a workload is labelling it, not
having its manifest edited.

Every workload in the listing produces a record. An unlabelled workload is
reported as `ungoverned` rather than skipped, an explicit opt-out as
`opted_out`, and a label value that is neither spelling is refused instead of
resolving to a posture nobody declared.

Given an earlier inventory, the command reports what changed. A workload that
drops the label is an `opted_out` event and a workload that has left the cluster
is a `withdrawn` event; neither is a row that stops appearing.

The inventory is the same artifact `bernstein governance plan` diffs against a
playbook, so the posture check needs no second format, and its content hash is a
pure function of the listing's contents rather than its order.

A governed workload's OTLP spans reach the ingest boundary under the source
label `k8s:<namespace>/<kind>/<name>`, so the signed, chain-anchored receipt
names the workload the activity came from. The route stays out of the data path:
there is no admission webhook and no workload is blocked from starting.
