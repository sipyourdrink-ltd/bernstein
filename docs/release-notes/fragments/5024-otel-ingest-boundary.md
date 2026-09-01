## OTLP Ingest Boundary with Anchored Receipts (Issue #5024)

Bernstein now introduces an OTLP ingest boundary with anchored receipts that accepts governance decisions and audit events emitted as OTLP by foreign runtimes and issues an anchored receipt over what it received.

Key capabilities:

- **Source identity in receipts**: Each receipt maintains source identity so different sources cannot produce interchangeable receipts
- **Honest coverage limits**: Clearly states Bernstein did not schedule foreign activity
- **Arrival vs. claimed order tracking**: Separates arrival order from claimed order to detect reordering
- **Profile-driven mapping**: Uses profile-driven mapping with no vendor branches
- **Offline receipt verification**: Verifies receipts using only receipt data and public key, without requiring the original OTLP payloads
- **Atomic payload handling**: Malformed OTLP payloads raise OTLPIngestError and append nothing to any chain, ensuring no partial state

This feature enables foreign runtimes to emit governance decisions and audit events as OTLP, with Bernstein providing verifiable receipts that can be independently verified offline.