## Honour raised max_agent_runtime_s as an agent deadline floor

`tuning.orchestrator.max_agent_runtime_s` once again reaches the spawned-agent watchdog when raised above its shipped 1800-second default. Raised values act as a floor under the existing scope/XL deadline buckets, while default or lower values leave the current 900/1800/3600/7200-second buckets unchanged and never shorten a longer bucket (#5379).
