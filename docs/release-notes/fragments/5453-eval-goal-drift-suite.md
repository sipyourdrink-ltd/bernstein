## Goal-drift benchmark suite and trajectory evaluation

Added the `goal-drift-v1` evaluation benchmark suite (`CTRL-GOAL-ALIGNMENT`, `ASI01`) with >= 10 canonical fixtures with planted distractions (TODO scope creep, tempting refactors, unrelated failing tests, stale docs, and premature optimizations). The suite evaluates agent trajectories against explicit `DriftContract`s and deterministically measures out-of-scope file modifications, forbidden changes, and hard drift curves from lineage events and diffs without model calls (#5453).
