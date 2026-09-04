## The govern inventory records when it last saw each surface

`Inventory` was a static tuple assembled once: no timestamp, no upsert,
and nothing that removed or demoted an entry. `govern discover` runs
repeatedly against the same targets, so an entity unplugged six months
ago looked identical to one seen five minutes ago, and a second pass grew
a duplicate row for every surface.

`Surface.observed_at` now carries the last observation instant, and
`Inventory.upsert` replaces under the stable id in place rather than
appending. `Inventory.sweep(older_than=, now=)` moves surfaces not
re-observed inside the window to a tombstone partition and returns what
it moved for the caller to journal. Nothing is hard-deleted, so "when did
we stop seeing X" is answerable from the record; an upsert of a
tombstoned id restores it.

`content_hash` excludes `observed_at` and the tombstones, so two
overlapping discovery passes over an unchanged environment converge to
one hash. `observed_at` defaults to `0.0`, so a document written before
this change loads and reads as never re-observed (#5083).
