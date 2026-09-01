## `bernstein govern inventory --render` (Issue #5133)

`bernstein govern inventory --render mermaid|dot` walks the inventory store and
prints a topology graph. Nodes and edges are sorted first, so the same store
produces the same bytes. The docs diagram is the mermaid render of a committed
fixture store; a test fails when that page drifts.
