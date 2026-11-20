## Document the ArtifactSink storage contract (#7169)

Add a short protocol reference for `ArtifactSink` to
`docs/operations/artifacts.md`, covering logical key naming, current async
method signatures (`write`, `read`, `list`, `delete`, `exists`, `stat`,
`close`), lexicographically sorted `list()` results, and the `durable=True`
write guarantee.
