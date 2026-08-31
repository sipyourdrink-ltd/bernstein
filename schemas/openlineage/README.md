# Vendored OpenLineage schema

JSON Schema for OpenLineage RunEvent validation, vendored so export
conformance never fetches anything at load time (air-gap).

| File | Validates |
|---|---|
| `1-0-5/OpenLineage.json` | OpenLineage 1-0-5 RunEvent (and related `$defs`) |
| `1-0-5/BernsteinChainRunFacet.json` | Bernstein custom run facet (`bernstein_chain`) |

- Upstream source: https://openlineage.io/spec/1-0-5/OpenLineage.json
- Spec version: 1-0-5
- Retrieved: 2026-09-01

Do not hand-edit `OpenLineage.json`. To move to a newer spec version, vendor
it as a new `schemas/openlineage/<version>/` directory and pin exporters/tests
explicitly.

`BernsteinChainRunFacet.json` is first-party: the custom run facet that binds
chain head hash, lineage record id, and detached projection signature so an
exported event is checkable against the WAL chain it came from (issue #4914).
