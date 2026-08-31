## Sandbox pool and admission limits pool separation

`bernstein pool` and `bernstein limits pool` maintain deliberate domain separation across distinct backing stores (#3138). `bernstein pool` governs named sandbox execution environments (manifests, backend allowlists, capability ceilings, egress classes) projected from the HMAC audit chain and content-addressed store. `bernstein limits pool` governs lease-backed admission slot pools (concurrency ceilings, posture enforcement) recorded in the hash-chained admission work ledger. Neither command group aliases, mutates, or shadows the other.
