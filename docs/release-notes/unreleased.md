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
