# Upgrade-task ownership contract

## Scope

`bernstein.evolution.upgrade_targets` implements the ownership half of issue
#3398: an auto-spawned upgrade task must declare the files its upgrade
touches, and the declaration must be provably the same thing the upgrade
executor writes.

`UPGRADE_CATEGORY_TARGETS` is the single table mapping each
`UpgradeCategory` to its target files, split by anchor: paths under the
runtime state directory (`.sdd` by the evolution loop's contract) and paths
under the repository root. Two resolvers render it:

- `upgrade_target_paths(category, state_dir)` - paths anchored on the
  executor's state directory (absolute exactly when `state_dir` is);
  `FileUpgradeExecutor`'s apply methods resolve their write targets through
  it, so the executor cannot write a file the table does not name.
- `upgrade_owned_files(category)` - workdir-relative strings;
  `_create_upgrade_tasks` posts them as the task's `owned_files`, and
  `UpgradeProposal.to_task()` carries the same derivation.

## Why declaration must equal execution

Every file-collision guard treats an empty `owned_files` as a valid "no
scope declared" and short-circuits: the ownership-overlap check reports
nothing, the batch-session claim path acquires no lock, and the
circuit-breaker scope check skips the session. Before #3398 upgrade tasks
always spawned with the empty value, so the guards were no-ops for exactly
the tasks the self-evolution loop creates unattended. Declaring paths that
are *not* what the upgrade touches would be worse than declaring nothing:
the guards would stop short-circuiting while still matching no real file.
`tests/unit/test_upgrade_targets.py` therefore runs the real executor per
category into a temporary workdir and asserts the touched files equal the
table's resolution.

## Degraded mode

A category missing from the table records why in the run's errors and still
spawns the task. Refusing creation was tried in #3397 and reverted: when the
derivation source is empty by construction, refuse-on-empty collapses into
"never spawn". Detector component labels ("model_routing", "policy",
backend names) are risk metadata for the proposal's blast radius and are
never written into `owned_files`.
