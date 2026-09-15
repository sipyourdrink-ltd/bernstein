## Signed chain-native memory recall receipts

Chain-native exact memory recalls can now be sealed as content-addressed signed receipts that bind the query, historical fold head, canonical fold hash, and ordered selected record hashes. Offline verification checks the detached JWS, operator HMAC, audit mirror, and deterministic replay; the existing SQLite spawned-agent recall path remains unchanged (#2914).
