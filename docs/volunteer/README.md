# Volunteer program

The volunteer program lets community members donate compute cycles toward
open-source backlogs that have opted in. A project declares a policy in
`.bernstein/volunteer.json`; the donor workflow derives a sandbox profile from
that policy. The protocol defines a signed receipt bundle that a maintainer
can verify offline.

## Start here

- [Volunteer task runner](reference/volunteer-runner.md) — how a claimed task
  is executed end to end, and how each stage can refuse.
- [Volunteer project manifest](reference/volunteer-manifest.md) — the schema
  a project commits to opt in.
- [Volunteer sandbox profile](reference/volunteer-sandbox.md) — the containment
  boundary derived from the manifest and the donor's limits.
- [Volunteer donor budgets](../reference/volunteer-budget.md) — persistent task,
  wall-clock, token, size, and local-model limits.
- [Volunteer protocol documents](protocol.md) — the five signed, transport-neutral
  documents (project card, worker card, claim, submission, verification
  verdict) that carry the cycle across GitHub and a self-hosted hub alike.

## For donors

Donors can browse opt-in projects, verify a project's manifest, and configure persistent donor limits from the CLI.
The claimed-task runner starts a host process in a Git worktree with a
filtered environment and wall-clock cap. See the
[volunteer sandbox profile](reference/volunteer-sandbox.md) for the profile
inputs and current enforcement.

```bash
bernstein volunteer budget --budget-tasks 10 --budget-hours 6 --max-size m
bernstein volunteer browse --index https://registry.example/index.json
bernstein volunteer verify /path/to/checkout   # check a project's manifest
```

## For project maintainers

A project needs only a committed `.bernstein/volunteer.json`. There is no
account to create and no service to register. `bernstein volunteer verify`
reproduces the manifest digest that receipts carry as `manifest_sha256`, so a
submission verifies against the policy the project actually declared.

```bash
bernstein volunteer verify
bernstein volunteer browse --index https://registry.example/index.json
```

## CLI surface

| Command | Purpose |
| --- | --- |
| `bernstein volunteer verify [repo]` | Validate a manifest and print its digest. |
| `bernstein volunteer browse --index URL` | List projects from a volunteer index. |
| `bernstein volunteer budget` | Set or inspect persistent donor limits and usage. |
| `bernstein volunteer autopilot` | Load an autopilot profile. Running tasks requires a Python task-source implementation. |
| `bernstein volunteer hub` | Serve the lease store and the hub's own task board over HTTP. |

## Hub task board

A hub can offer work that has no git-forge issue behind it. `POST
/volunteer/tasks` publishes one (operator scope `volunteer:publish`) and `GET
/volunteer/tasks` lists the board to a caller holding `volunteer:claim`; a
donor then claims, heartbeats, and submits through the endpoints it already
uses. The board's log is a sibling of the lease log, so a hub that is torn down
and brought back still offers what it offered.

A hub-native task id is `hub:` followed by the sha256 of the task's content.
The prefix is reserved, so an id mirrored from a git forge can never be
mistaken for a board one, and a donor can recompute the digest from the content
it was handed to confirm it matches the id it holds a lease on. Republishing
identical content lands on the id that already exists rather than doubling the
board.
