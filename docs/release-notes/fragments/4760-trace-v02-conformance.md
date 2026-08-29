## Trust records conform to the TRACE v0.2 schema

Emitted trust records now carry the v0.2 claim surface — SPIFFE subjects derived from the run journal, `policy.enforcement_mode`, closed `runtime`, build provenance, and an appraisal naming an external verifier. Delegated executions link to their parent by record hash, and a run-level aggregate references its member executions. Four checked-in vectors are validated against the vendored schema, the reference model, and the reference conformance suite at Level 0 on every test run (#4760).
