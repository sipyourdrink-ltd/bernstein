## Pull requests are described from the change, not from the session

`bernstein pr` took its title from whatever the run committed last, so a run
that ended with a lint repair or a formatting pass named the whole pull request
after that upkeep and the feature it had implemented was invisible to reviewers
and to the changelog. The body had the same problem from the other end: it
opened with the session's own status text instead of saying what the diff does.

The title now names the commit that changed the most under `src/`, skipping
merge, `[WIP]`, `style:`/`chore:`, formatter and lint-repair commits and
generated-context-file syncs, and falls back to the linked issue's title when a
run left nothing but upkeep. The body is composed from the issue's problem
statement, the files the diff touches and the gates that ran, and carries a
Provenance block naming the diff hash and the run's journal head. Opening a pull
request anchors that description through the existing review-receipt machinery,
so `bernstein review-receipt verify` accepts a description that matches its diff
and rejects one whose diff has since changed. `--title` and the new `--body`
still override everything (#4484).
