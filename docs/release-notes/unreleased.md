# Unreleased

Changes merged to `main` that are not yet part of a tagged release. Each
tagged release has its own page in this directory; this page carries what has
landed since the newest one.

Cutting a version empties this page: every entry the tag ships moves onto that
version's page in the release PR itself. `tests/unit/test_unreleased_notes_rotation.py`
holds the page to that — an entry naming an issue or PR a tagged release page
already documents fails the build. An entry that cites released work as context
rather than as its own attribution is exempted by hand there, with the reason.

- The repo's own run seed ran the lint gate as a bare `ruff check .`. This
  project is uv-managed and never activates its virtualenv, so the gate shell
  could not find the binary and reported "ruff: not found" as a lint failure on
  every run, blocking merges for a reason no code change could fix. The seed now
  invokes ruff through `uv run`, and a test holds every gate command that names
  a venv-resident tool to that form. (#4547)

- `shipped bundle matches the lockfile` became a required status check on
  `main` while its `pull_request` trigger still filtered on `web/**`. A
  required context only reports for the events its trigger accepts, so every
  pull request outside that filter waited on a run that never started and
  could not merge at all. The trigger no longer filters, the checked-in
  ruleset mirror now matches the live one, and the merge-queue runbook carries
  the trigger precondition as a numbered step so the next lane to be required
  cannot repeat it. (#4556)
- A `test_passes` completion signal names its test by path, and that path is
  written when the task is planned rather than read off the suite. A plan
  mirroring `src/bernstein/core/security/` asked for
  `tests/unit/core/security/test_policy.py` while the file lives at
  `tests/unit/test_policy.py`, so pytest exited during collection and the
  janitor rejected the task over a path the agent never chose. The command is
  now resolved against the tree first, following only an unambiguous rename;
  a basename that is absent everywhere or matches several files keeps the
  original path so the command still fails honestly. (#4554)
