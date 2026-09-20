## OSCAL v1.1.0 assessment-results export

`bernstein compliance export-oscal` (and the underlying
`bernstein.compliance.oscal` module) writes a NIST OSCAL v1.1.0
assessment-results JSON document from the tamper-evident audit chain,
lineage log, and cost ledger. The vendored schema validates the output.
Closes #5890.