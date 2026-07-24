# Multi-cell orchestration

Split one run into several independently-scheduled **cells** - each with its
own manager and worker pool - coordinated by a VP layer that watches
cross-cell status on the bulletin board and rebalances when a cell is
overloaded or stuck.

## Why

A single manager/worker pool scales fine up to a few dozen open tasks. Past
that, one manager becomes a bottleneck: task grouping, spawn-capacity checks,
and dead-worker reaping all run against one queue, and a stall in one
subsystem (say, a flaky backend integration) crowds out unrelated frontend
work waiting in the same queue. Multi-cell orchestration splits the backlog
into per-subsystem cells so each has its own manager, its own worker cap, and
its own tick - with a VP cell above them that only handles cross-cell
concerns.

## How to use it

```
bernstein run --cells 3
```

`--cells` (default `1`) sets the number of parallel orchestration cells. `1`
keeps the existing single-cell orchestrator; any value greater than `1`
spawns a `MultiCellOrchestrator` instead. The flag overrides a `cells:` value
in the seed file only when explicitly passed with a value greater than `1`.

There is no separate command to register a cell or assign tasks to one by
hand - cell membership comes from how the backlog is decomposed (each task
carries a `cell_id`) and the orchestrator fetches open tasks per cell via
`GET /tasks?status=open&cell_id=<id>`.

## How it behaves

Each `tick()` of the `MultiCellOrchestrator` does four things, in order:

1. **Drain the bulletin board.** New messages posted since the last tick are
   read; any of type `blocker` are logged and, if a
   `ClearanceGateCoordinator` is wired, materialized into a clearance task
   plus injected `depends_on` edges (see [Cross-worker
   coordination](worker-coordination.md#blocker-clearance-gates) for how that
   projection and its receipt work). Without a coordinator wired, blockers
   stay observe-only: logged but not turned into a scheduling gate.
2. **Tick every registered cell.** For each cell: fetch its open tasks, group
   them into role batches with fair scheduling, spawn agents up to the
   cell's `max_workers + 1` (the `+1` accounts for the cell's own manager),
   and reap workers that are dead or past `heartbeat_timeout_s`.
3. **Check for rebalancing.** After every cell has ticked, the VP layer scans
   per-cell status snapshots and raises an action for any cell with more than
   15 open tasks (posted as an `alert`) or more than 3 blocked tasks (posted
   as a `blocker`). These are advisory: the VP logs and posts to the
   bulletin board, it does not itself move tasks or spawn a new cell.
4. **Write a one-line summary** to `.sdd/runtime/multi_cell.log` (cell count,
   total open tasks, agents spawned, blockers found, VP actions, errors).

Cells are independent `Cell` objects tracked in memory by `cell_id` -
`register_cell()` / `remove_cell()` add and drop them. A cell reaping a dead
worker or failing a fetch does not stop the tick for other cells: per-cell
errors are collected into `MultiCellTickResult.errors` and the loop continues
to the next cell.

`MultiCellOrchestrator.run()` blocks the calling thread and calls `tick()` on
`OrchestratorConfig.poll_interval_s` intervals until `stop()` is called.

## Limitations

- The overload/blocked-task thresholds (`>15` open, `>3` blocked) are fixed
  in code, not configurable per project.
- Rebalancing is advisory only: the VP posts an alert or a blocker signal but
  does not automatically split an overloaded cell or move tasks between
  cells. An operator (or a downstream automation reading the bulletin board)
  has to act on it.
- Multi-cell orchestration runs cells in one process against one task
  server. It is not the same mechanism as multi-host fan-out - that is
  `bernstein worker` / cluster mode, which distributes work across separate
  machines rather than separate cells within one run.

## Source

- `src/bernstein/core/orchestration/multi_cell.py` - `MultiCellOrchestrator`,
  `CellStatus`, `MultiCellTickResult`.
- `src/bernstein/cli/run_bootstrap.py` - the `--cells` flag on `bernstein
  run`.
- `src/bernstein/core/orchestration/bootstrap.py` - `bootstrap_from_seed()`
  resolving `cells` and passing it through to the spawner.

See also: [Cross-worker coordination](worker-coordination.md) for the
bulletin board and BLOCKER clearance-gate mechanics the VP layer uses when a
coordinator is wired.
