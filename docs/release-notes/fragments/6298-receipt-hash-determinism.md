## Read-set refusal receipt hash no longer depends on `changed_paths` order

`ReadSetRefusalReceipt.canonical_bytes()` relied on `sort_keys=True`, which orders dict keys but not list items, so the same logical refusal hashed differently when `changed_paths` arrived in a different order (for example from set iteration under different `PYTHONHASHSEED` values). `changed_paths` is now emitted sorted by `(path, old_commit, new_commit)`. The wire format is otherwise unchanged; receipts already minted with unsorted paths keep their stored signature but their recomputed hash may differ (#6298).
