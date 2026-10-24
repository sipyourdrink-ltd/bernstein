## Activate cost-anomaly lifecycle checks

Cost-anomaly stop signals now pause new spawns until the operator calls
`Orchestrator.resume_spawning()`. Completion checks receive task cost, token
counts and retry lineage and update the appropriate complexity baseline.
Single-task spawn checks use the spawner's resolved model, including role and
template defaults, provider failover, bridge and resume attempts. The existing
`CostAnomalyConfig.enabled` default is true; explicitly disabling it keeps
these checks inert. (#5942)
