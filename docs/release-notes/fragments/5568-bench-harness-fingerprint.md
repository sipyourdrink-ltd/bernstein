## Bench result bundles carry harness fingerprint and drift gate

Submission bundles now compute and persist a deterministic `harness_fingerprint`
over canonical run-shaping settings (`decomposition`, `effort`, `prompt_templates`,
`retry_policy`, `sandbox`, `timeouts`, `tool_allowlist`). The new `bernstein bench
compare` command refuses to rank bundles produced with differing harness configurations
unless `--allow-harness-drift` is passed, and leaderboards group submissions by
harness fingerprint prefix.

(#5568)
