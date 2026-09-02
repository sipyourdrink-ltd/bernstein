# `bernstein govern inventory --render`

A diagram maintained by hand is stale the day after it is drawn. This command
walks the inventory store and prints a topology graph.

```bash
bernstein govern inventory --render mermaid --store tests/fixtures/govern/inventory-store.json
bernstein govern inventory --render dot --store path/to/store.json
```

`--render` takes `mermaid` or `dot`. Nodes sort by id and edges by
`(from, to)`, so the same store produces the same bytes. There is no extra
layout or styling: Mermaid is a `flowchart TD` with positional node ids
(`n0`, `n1`, …) so punctuation in store ids cannot collide; DOT is a
`digraph inventory` that quotes the raw ids.

`--store` is a JSON file with `nodes` and `edges` (a hand-written fixture until
the entity-per-file store in #5129 lands):

```json
{
  "nodes": [{"id": "host_dev", "label": "developer machine"}],
  "edges": [{"from": "host_dev", "to": "agent_claude"}]
}
```

The docs diagram at [inventory topology](../diagrams/inventory_topology.md) is
the mermaid render of the committed fixture. A unit test fails when that page
drifts. Mermaid is the CI-gated format because GitHub and MkDocs render it
without Graphviz; `dot` still ships on demand.

Exit codes: `0` emitted, `1` store unreadable or not a JSON object, `2` usage
(missing `--render` / `--store`).

See also [`bernstein graph tasks --format mermaid`](cli-reference.md) (task DAG,
not this store) and [govern verify](governance.md).
