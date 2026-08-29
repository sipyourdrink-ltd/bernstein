# Volunteer program

The volunteer program lets community members donate compute cycles toward
open-source backlogs that have opted in. A project declares a policy in
`.bernstein/volunteer.json`; a donor's worker derives a sandbox profile from
that policy, runs a task in an isolated worktree, and produces a signed
receipt bundle that a maintainer can verify offline.

## Start here

- [Volunteer task runner](reference/volunteer-runner.md) — how a claimed task
  is executed end to end, and how each stage can refuse.
- [Volunteer project manifest](reference/volunteer-manifest.md) — the schema
  a project commits to opt in.
- [Volunteer sandbox profile](reference/volunteer-sandbox.md) — the containment
  boundary derived from the manifest and the donor's limits.
- [Volunteer donor budgets](reference/volunteer-budget.md) — persistent task,
  wall-clock, token, size, and local-model limits.

## For donors

A donor runs `bernstein volunteer` against an issue labeled for volunteers.
The worker never inherits the host environment, never runs a shell on issue
text, and never loosens containment — the profile digest that lands in the
receipt is a pure function of the project's manifest.

```bash
bernstein volunteer budget --budget-tasks 10 --budget-hours 6 --max-size m
bernstein volunteer run https://github.com/owner/project/issues/123
bernstein volunteer verify /path/to/checkout   # check a project's manifest
```

## For project maintainers

A project needs only a committed `.bernstein/volunteer.json`. There is no
account to create and no service to register. `bernstein volunteer verify`
reproduces the manifest digest that receipts carry as `manifest_sha256`, so a
submission verifies against the policy the project actually declared.

```bash
bernstein volunteer verify
bernstein volunteer browse            # list issues open to volunteers
```

## CLI surface

| Command | Purpose |
| --- | --- |
| `bernstein volunteer verify [repo]` | Validate a manifest and print its digest. |
| `bernstein volunteer browse` | List issues labeled for volunteers. |
| `bernstein volunteer budget` | Set or inspect persistent donor limits and usage. |
| `bernstein volunteer run <url>` | Execute a claimed task and produce a receipt. |
| `bernstein volunteer hub` | Serve the lease store over HTTP (see [issue #4037]). |
