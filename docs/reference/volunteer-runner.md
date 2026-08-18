# Volunteer task runner

`run_claimed_task` takes one claimed task — a repository URL, an issue, and a
validated [manifest](volunteer-manifest.md) — and returns either a diff or a
structured refusal. It is the wiring between three primitives that already
existed and were never called together: the
[sandbox profile](volunteer-sandbox.md), the wall-clock cap, and worktree
isolation. It adds no containment of its own.

## The pipeline

| Step | What happens | How it can refuse |
|---|---|---|
| 1. Profile | The containment boundary is derived from the manifest and the donor's limits. | `sandbox_profile` — carries the profile primitive's own reason code |
| 2. Repository URL | The scheme is checked against an allowlist before git is invoked. | `repo_url` / `unsupported_repo_url` |
| 3. Clone | Shallow, single-branch, under the run budget, with no host credentials. | `clone` / `clone_failed`, `clone_timed_out` |
| 4. Worktree | `WorktreeManager.create` builds the isolated checkout. | `worktree` / `worktree_failed` |
| 5. Prompt | Sanitized issue text is written to a file inside the worktree. | `prompt` / `prompt_unwritable` |
| 6. Agent | Spawned under the remaining budget with the profile's environment. | `agent` / `wall_clock_exceeded`, `agent_failed` |
| 7. Diff | `git diff` against the commit the worktree started at. | `diff` / `empty_diff` |

Step 1 comes first on purpose. Deriving the profile is pure and free; fetching
a stranger's repository is neither. A host that cannot contain this project
never pulls the repository onto its disk.

Nothing in the pipeline raises at the caller. A refusal is a value with a
stable reason code, because refusals are the ordinary outcome on a mixed fleet
of donor machines and a refusal that arrives as an exception is one somebody
has to guess how to catch.

## What "inside the sandbox" covers, and what it does not

A profile decides four things: backend, egress, environment and resources. The
runner applies two of them — the environment the agent process is given, and
the wall clock it runs under — to a process started **on the host**, inside the
isolated worktree.

It does not yet place that process inside the backend the profile selected.
Standing up a sandbox, mounting the worktree into it and collecting the result
back out is a change against the backends rather than against this wiring.
Until it lands, a run on this path is contained by an environment with no
credentials and a cap that kills the process tree, and it is **not** contained
by a kernel boundary. Stated here rather than implied, because a reader who
takes "runs inside the volunteer sandbox" to mean process-level isolation has
been told something untrue.

## One budget for the whole run

A donor lends N minutes of their machine, not N minutes per phase. A ceiling
applied per phase would let a run with an agent phase and *k* gate phases hold
the machine for (k+1)×N.

So `WallClockBudget` starts at the profile's ceiling and is spent down across
every phase. A caller continuing the pipeline — into the project's gates, say —
passes the same budget on, and it is **clamped to the profile's ceiling**: a
caller can tighten the loan and never loosen it. Seconds are fractional
throughout, because a budget rounded down at every hand-off silently shortens
the loan and one rounded up hands out time nobody lent.

## Untrusted text never becomes an argument

The issue title and body reach the agent as a **file** in the worktree, wrapped
as clearly-delimited data. They are never placed in an argument vector and
never in an environment variable, and nothing runs through a shell.

That is enforced by the shape of the launcher seam rather than by discipline.
A launcher is handed an `AgentInvocation` — working directory, prompt path, log
path, session id — and no issue text at all, so it cannot place any into a
command line even by accident.

Prompt wrapping is a mitigation layered on top of that, not a substitute for
it. The boundary is the file, the credential-free environment, and the egress
the project declared.

## The agent is spawned by the runner, not by an adapter

`CLIAdapter.spawn()` takes no environment — every adapter builds its own, from
the allowlist that exists to carry *provider credentials* into an adapter
process, which is the opposite of what this boundary wants. It also owns its
own process handle, and the wall-clock cap owns its process handle end to end,
so the two do not compose.

So the runner owns the spawn and takes an argv-builder instead. Both properties
then hold by construction: one process, started by the wall clock, with an
environment built only from the profile. `mock_agent_argv(fix=...)` is the
zero-key builder used by tests and demos; it runs the mock adapter's own
program text, and its scripted-fix selector is supplied by the caller rather
than read from the issue title — issue text does not enter an argument vector,
and an exception for the convenient case is how that stops being true.

## The clone gets its own narrow environment

The repository URL comes from a claimed task, so the clone is a trusted program
pointed at an untrusted host. It runs with:

| Control | Why |
|---|---|
| `HOME` redirected into the run's workspace | No `~/.gitconfig`, so no credential helper the donor configured for their own work, no `url.<x>.insteadOf` rewrite, no ssh keys |
| `GIT_TERMINAL_PROMPT=0` | An authentication challenge fails fast instead of blocking on a prompt nobody will answer |
| `GIT_CONFIG_NOSYSTEM=1` | The system config cannot re-enable a transport or a helper |
| `GIT_ALLOW_PROTOCOL` | git refuses a transport outside the allowlist independently of the URL check |
| Everything else dropped | Tokens and agent sockets are absent by construction, not by filtering |

## What the caller gets

`TaskDiff` carries the patch, the worktree it came from, the base commit, the
manifest and profile digests, the wall-clock outcome, and what is left of the
budget. `TaskRefusal` carries the stage, the reason code, the detail, and the
same digests.

The worktree is left in place. The diff's provenance is that worktree, and the
step that enforces `allowed_paths` and re-runs the project's gates verifies
against it; deleting it here would destroy the thing that step needs.

## Claim etiquette is etiquette, not a lock

The PoC has no coordinator, so two donors can pick the same issue off a
project's backlog. Passing a claim client to `run_claimed_task` adds the one
cheap defence available without a lock: before the clone, the runner re-reads
the issue and **refuses on the `claim_taken` stage** if it is assigned, closed,
or carries a claim comment newer than a configurable staleness window that
another donor wrote. Otherwise it posts a short claim comment, and on abort
edits that same comment to a release; the completion step
(`finish_volunteer_task`) edits it to a completion. It is entirely opt-in —
with no claim client the runner behaves exactly as before.

**This is best-effort, and races are accepted.** Two donors reading the issue
in the same second both see it free and both claim it; nothing here prevents
that, and at PoC scale occasional duplication is fine. What the check buys is
that the *second* donor to arrive a minute later sees the first one's claim and
steps aside — the difference between duplicating a task now and then and
silently duplicating every task.

Two properties make it honest without a coordinator:

- **The donor's own `gh` auth.** Every read and write is a `gh` subprocess that
  authenticates as whoever ran it — never an installed GitHub App. A donor
  volunteering on someone else's project comments as themselves, and the runner
  never mutates labels or state it lacks the rights to.
- **`viewerDidAuthor`, never a stored login.** The claim is found by a marker
  embedded in the comment body, scoped to comments the active identity authored
  (`gh issue view --json comments` reports `viewerDidAuthor` per comment). So a
  worker edits only its own claim — GitHub answers a cross-author edit with 403 —
  and a restarted worker that finds its *own* fresh claim resumes rather than
  treating itself as a competitor and locking itself out.
