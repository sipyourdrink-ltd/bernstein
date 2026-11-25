Resolve retry models from the same source the first attempt used on
gateway-only configurations (#6605). When the retrying role's adapter is not
Claude-compatible (an OpenAI-compatible gateway via `OPENAI_API_BASE`, or a
non-Claude `role_model_policy` provider), both retry creators now skip the
Claude tier ladder: an unpinned bare tier label (`sonnet`/`opus`/`haiku`) is
left unset so the spawn re-resolves the adapter's configured default, and no
other unprefixed model name is escalated into a tier the endpoint cannot
route. Explicit role/run model pins, `metadata.pinned_model` comparison
pins, provider-prefixed models and the Claude-adapter ladder are unchanged.
