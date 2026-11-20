# Release notes

This directory holds three kinds of file:

| File | What it is |
|---|---|
| `vX.Y.Z.md` | A tagged release's page. Immutable once shipped. |
| `unreleased.md` | Entries that landed since the newest tag, added by hand. Kept only for the fragments transition; see below. |
| `fragments/<issue-or-slug>.md` | One pending entry, one file. The current way to add an entry (#4474). |

## Adding an entry

Add `docs/release-notes/fragments/<issue-or-slug>.md` in the same PR as the
change. Name it for the issue or PR it documents (`4474-notes-fragments.md`
is fine; so is a short slug when there is no issue). Content is exactly what
an entry has always been: a `## <title>` heading and a short prose paragraph,
the issue or PR number in parens at the end.

```markdown
## Two independent entries no longer share a file

Every PR appended one line to `unreleased.md`, so any two open PRs conflicted
on that file in the merge queue. An entry is a fragment file now (#4474).
```

One file per entry means two PRs that each add a fragment never touch the
same line, so they merge through the queue in either order with nothing to
rebase.

Editing `unreleased.md` directly still works during the transition -- the
release-notes gate (`scripts/rotate_release_notes.py check-gate`) accepts
either form.

## Cutting a release

For v4.0.0, start from the pre-cut tree and run these commands with the
project environment active. Set `SOURCE_DATE_EPOCH` to the release date in UTC
(Unix seconds) before the version bump so the citation date is reproducible.

```sh
python scripts/rotate_release_notes.py assemble v4.0.0
python scripts/bump_version.py 4.0.0
```

The first command reads the unreleased entries and the fragments in filename
order. Its inputs in `scripts/release_cuts/v4.0.0.toml` identify deferred
fragments, links to relocate, wording corrections and known limitations.
It adds the new page to navigation and removes only the fragments it used.

For a manually curated version, `scripts/rotate_release_notes.py rotate
docs/release-notes/vX.Y.Z.md` deletes consumed fragments once the page
exists. Move any entries from `unreleased.md` to that page in the same PR.
