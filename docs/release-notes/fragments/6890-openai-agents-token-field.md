## Correct OpenAI Agents request defaults

The OpenAI Agents adapter no longer sends the routing budget as an implicit
output limit. It omits incompatible mode-profile sampling defaults for GPT-5
mini and Kimi K2.5, while preserving explicit settings on custom endpoints.
GPT-5 mini completion limits use `max_completion_tokens` on custom Chat
Completions endpoints. The standard Responses route and other models retain
their existing token fields. Explicit invalid custom-endpoint settings are
still provider-validated (#6890).
