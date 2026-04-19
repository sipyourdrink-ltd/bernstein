# Cell decomposition

When deciding how to split work across cells:

- Each cell owns a coherent subsystem (auth, API, ML pipeline, frontend, …).
- Minimise cross-cell dependencies.
- Each cell's work should be independently testable.
- Prefer vertical slices (full feature in one cell) over horizontal splits
  (layers across cells).

## Signs a cell is too big
- Manager spends more time routing tasks than reviewing outputs.
- Multiple sub-domains share an owner.
- Single-task latency grows because the backlog dominates routing.

## Signs a cell is too small
- One or two workers idle each cycle.
- Manager burns cycles creating filler tasks.
- Scope overlaps another cell's remit — they will collide on files soon.

## When you redistribute work
- Freeze in-flight tasks; move only the ones that haven't started.
- Update `.sdd/cells.yaml` with the new ownership map.
- Announce the change on the bulletin board so Managers don't double-book.
