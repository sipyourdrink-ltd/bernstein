# Compare two render receipts offline

Run the example from the repository root.

```sh
uv run python examples/render-pairs/demo.py
```

The script constructs the receipts in memory and passes them to
`render_delta()`. It runs without a browser or a network connection.

In `changed`, both receipts declare the same environment. The title's
`style.color` changes from `navy` to `teal`. In `incomparable`, the head
receipt declares a different locale. The result gives the environment
descriptor mismatch in `incomparable_reason` and returns an empty delta
list. Both results include `base_unstable_properties` and
`head_unstable_properties` as recorded on the two receipts.

The smoke test checks both comparisons and compares stdout across two runs.

```sh
uv run pytest -q tests/unit/examples/test_render_pairs_demo.py
```
