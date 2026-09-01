## gptme honouring `max_tokens`

The gptme adapter now wires `mcp_config["max_tokens"]` through to the
per-process `GPTME_MAX_TOKENS` environment override instead of leaving it
unused. The adapter declares the narrow `SUPPORTS_MAX_TOKENS` capability, so
the sampling gate admits a spawn carrying `max_tokens` for gptme rather than
refusing it (the value was previously refused and honoured nowhere).
`GPTME_MAX_TOKENS` is set on the filtered environment after
`build_filtered_env`, not passed through `extra_keys`, so an ambient shell
variable cannot leak through. #4955
