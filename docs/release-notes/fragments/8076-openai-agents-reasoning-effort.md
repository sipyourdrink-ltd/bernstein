## openai_agents sends the configured reasoning effort

The `openai_agents` runner built its SDK `Agent` without
`ModelSettings.reasoning`, so a resolved `effort=max` never reached the
Responses request and the provider default applied. The runner now sends the
manifest's effort as `reasoning.effort` for reasoning models on the default
OpenAI endpoint, logs the value it sent, and refuses an unknown effort before
the call (#8076).
