## CycloneDX documents move to 1.7, and the serial number is a UUID URN

Emitted CycloneDX documents now declare `specVersion` 1.7 -- the release that
carries the ML-BOM fields -- and each AI BOM gains a `modelCard` projecting only
the facts the run recorded. The serial number changed shape: the old
`urn:uuid:bernstein-ai-bom:<run_id>` form is not a UUID URN and the
specification schema rejects it, so anything that correlated BOMs by parsing the
run id out of the serial loses that key. The replacement is
`metadata.properties[bernstein:run_id]`, alongside `bernstein:started_at`,
`bernstein:finished_at` and `bernstein:lineage_root_hash`; the serial itself is a
uuid5 over a bernstein-owned namespace and so stays stable for a given run. A
run-shaped dependency SBOM derives its serial the same way, under its own
namespace, when a run id is supplied -- without one it keeps a random UUID URN.
Documents are validated against the vendored official schema in CI now, which is
what caught the invalid serial (#6218).
