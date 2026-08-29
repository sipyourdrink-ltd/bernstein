# Volunteer Program: Project Maintainer Guide

This guide covers what a project maintainer needs to do to participate in the
volunteer program. There is no account to create and no central registration.
Everything lives in your repository.

## Opting in with a manifest

A project opts in by committing `.bernstein/volunteer.json` to its default
branch. The file declares the policy the project requires of every volunteer
submission. See the [manifest schema reference](reference/volunteer-manifest.md)
for the full field list.

```json
{
  "version": 1,
  "license": "Apache-2.0",
  "gates": [["uv", "run", "pytest", "-q"]],
  "allowed_paths": ["src/**", "tests/**", "docs/**"],
  "egress_allowlist": [],
  "sandbox": "container",
  "max_wall_clock_minutes": 30,
  "task_label": "volunteer-ok",
  "local_ok": false,
  "status": "active"
}
```

A few fields deserve particular attention:

- `gates` must name at least one command as an argv array. A bare string is
  refused. Gate commands run without a shell.
- `task_label` is the issue label donors use to find work. Label your issues
  with this value to make them visible to the volunteer browser.
- `allowed_paths` restricts what a patch may touch. An empty list means
  repo-wide.
- `status` controls whether new submissions are accepted.

Validate your manifest before committing:

```bash
bernstein volunteer verify
# manifest_sha256: abc123...
```

The digest is stable across formatting changes (canonical JSON, sorted keys).
It is what a submission receipt carries as `manifest_sha256`.

## Making issues visible to volunteers

Label issues you want volunteers to work on with the value in `task_label`
(default: `volunteer-ok`). A donor runs `bernstein volunteer browse` to
discover labeled issues across their configured indexes.

There is no separate "volunteer issue tracker" — donors browse the repositories
they already monitor.

## What you receive

When a volunteer completes a task, they open a pull request from their fork. The
PR includes:

- A signed receipt bundle attesting which gates ran and what they produced.
- The patch itself, touching only files within your `allowed_paths`.
- The `manifest_sha256` the submission was produced against.

The receipt is verifiable offline:

```bash
bernstein volunteer verify /path/to/checkout
# recomputes the digest from your committed manifest
# if it matches manifest_sha256, the gates ran your declared policy
```

A mismatch means the volunteer ran against a different policy — not a failed
gate, but a policy the project did not declare. That receipt is refused with a
clear error, not silently accepted.

## Review workflow

A volunteer submission arrives as an ordinary fork PR. Review it the same way
you review any contribution:

- Does the change make sense?
- Does it meet the project's standards?
- Are the tests correct and complete?

The receipt only proves the gates passed; it says nothing about whether the
change is correct or desirable. You still own the call on what gets merged.

If you accept the work, merge the PR. If you do not, close it. The receipt
does not obligate you to merge.

## Pausing and leaving

### Pause accepting new work

Set `status` to `"paused"` in `.bernstein/volunteer.json` and commit. Donors
stop seeing your project in their browse list. Existing receipts remain
verifiable against the same digest.

```json
"status": "paused"
```

### Leave the program

Delete `.bernstein/volunteer.json` from your default branch. This removes your
project from donor discovery entirely. Existing fork PRs from volunteers are
not affected; they are pull requests like any other.

### Resume accepting work

Set `status` back to `"active"` or re-commit the manifest file. The digest
will change because the manifest at the commit where it is declared is what
receipts bind to.
