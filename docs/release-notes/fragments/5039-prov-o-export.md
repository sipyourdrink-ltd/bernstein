## PROV-O export for lineage v1 ancestry

`bernstein lineage export-prov <artefact>` projects an artefact's
`parent_hashes` ancestry from the v1 log into W3C PROV-O (PROV-JSON
or Turtle). Deterministic: two exports of the same ancestry produce
byte-identical output. (#5039)
