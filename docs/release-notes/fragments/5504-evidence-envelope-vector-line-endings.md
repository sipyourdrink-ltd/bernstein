## Evidence-envelope golden vectors are pinned to their exact bytes

`tests/fixtures/evidence-envelope-vectors/` holds the RFC 8785 canonical
JSON, the detached signature key and the `.sha256` sidecar that pin the
evidence-envelope format, and all three are compared as raw bytes. They
carried no `.gitattributes` rule, so a checkout with line-ending translation
enabled - the Windows default - could rewrite them on the way to disk and
fail the comparison against a file that is byte-correct in the repository.

The three sibling vector sets (`receipt-vectors`, `trust-record-vectors`,
`agent-card-utf16-vector`) already declare `-text` for exactly this reason;
`evidence-envelope-vectors` now does too.
