## Power-of-two-choices batch task assignment for cluster nodes

Added `batch_assign()` in `bernstein.core.protocols.cluster.cluster` to assign
batches of tasks across healthy online workers (`NodeInfo.is_schedulable`) with
model, GPU, and label-affinity filtering, in-memory slot accounting to prevent
intra-batch over-assignment, and power-of-two-choices load spreading (#6389).
