## OTLP Ingest Acceptance (Issue #4983)

Bernstein now accepts OTLP spans from existing instrumentation as the cheapest first step into governance. Operators can point their existing OTLP collector at Bernstein and the spans their agents already emit become chain-anchored governed activity.

Where spans carry the OpenTelemetry GenAI semantic conventions, they are extracted into typed ``GenAIActivity`` records with model and token counts. Spans without those conventions are recorded as ``UntypedActivity`` and never inferred.

The OTLP ingest receiver is a stateless component that parses OTLP/JSON payloads and maps each span to either a typed or untyped activity record. Malformed payloads raise ``OTLPIngestError`` and append nothing to any chain.

This feature enables operators with existing instrumentation to opt into governance without adding a second instrumentation path.