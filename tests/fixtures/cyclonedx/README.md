# Vendored CycloneDX schemas

Offline copies of the official CycloneDX JSON schemas, so the AI-BOM and SBOM
tests can validate emitted documents without network access.

Upstream: [`CycloneDX/specification`](https://github.com/CycloneDX/specification)
tag `1.7.2` (released 2026-09-17), path `schema/`.

| File | sha256 |
|---|---|
| `bom-1.7.schema.json` | `73308edec3ab2d38bfffd993e96a042b594314143b6971a6e9ed98bbb6bd76ce` |
| `spdx.schema.json` | `4b345e2329f209f34e960ae2a8e7cb46a166907e6a45e94978565925dc47b359` |
| `jsf-0.82.schema.json` | `8bae002c25e723db7ee1f26afde680ae1a2b1a8f6b4b4b0fd65dc3becb090aae` |

Refresh procedure: download the three files from
`https://raw.githubusercontent.com/CycloneDX/specification/<tag>/schema/<name>`,
re-check the digests above, and re-run
`tests/unit/compliance/test_ai_bom_cyclonedx_schema.py`.

`cryptography-defs.schema.json` is referenced by the BOM schema, but only from
the signature / crypto-reference definitions. The emitted documents carry no
`sig` or `cryptoRefArray` fields, and the validator resolves `$ref`s lazily,
so that file is deliberately not vendored.
