## Trust Record emitter for TRACE 0.2

New internal API (`bernstein.core.observability.trust_record.TrustRecordEmitter`)
maps a finished run's journal onto a signed TRACE 0.2 Trust Record using the
install Ed25519 identity. Optional extra `bernstein[trace]` adds the
`agentrust-trace` dependency; the core install remains unchanged without it.
Closes #4666.