## Worker completions can identify produced content

Worker completion payloads may now include content-addressed `exports` entries
that pair an artifact path with its canonical SHA-256 digest. Existing
`worker-completion/v1` payloads remain valid without the new field. #5002
