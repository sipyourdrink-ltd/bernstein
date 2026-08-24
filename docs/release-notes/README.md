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

`scripts/rotate_release_notes.py rotate docs/release-notes/vX.Y.Z.md`
concatenates the fragments in deterministic filename order, appends the
rendered section to the version page, and deletes the consumed fragments --
commit both edits together, the same way emptying `unreleased.md` has always
been a step of the release PR itself (`docs/operations/release.md`).
Entries still carried in `unreleased.md` continue to move onto the version
page by hand, exactly as before; `tests/unit/test_unreleased_notes_rotation.py`
fails the build if one gets left behind.
