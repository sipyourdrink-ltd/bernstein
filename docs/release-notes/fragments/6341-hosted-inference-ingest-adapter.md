## feat(observability): add HostedInferenceIngestAdapter for hosted inference calls

Add HostedInferenceIngestAdapter to accept externally-generated OpenAI-compatible inference calls as governed activity. This enables operators to instrument their agent workloads with direct API calls to hosted providers (like OpenAI, Anthropic, etc.) and send request/response metadata to Bernstein for governance and audit.

Closes #4966