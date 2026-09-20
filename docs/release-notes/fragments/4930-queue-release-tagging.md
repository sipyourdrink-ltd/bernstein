## A version bump that lands through the merge queue now tags

A release commit merged through the merge queue has no `push` CI run: the
queue fast-forwards `main` onto the SHA its `merge_group` run already built,
so the only completed `CI` run for that commit reports
`head_branch = gh-readonly-queue/main/pr-<n>-<base_sha>`.
`post-ci-dispatcher.yml` filtered those refs out, so `auto-release.yml` was
never invoked and the tag was never created - with no failing check to show
it.

The dispatcher now also listens on `gh-readonly-queue/main/**` and routes a
successful queue ref whose base is `main` to `auto-release.yml`. The release
gate confirms the triggering SHA is an ancestor of `main` before tagging, so
a queue entry that is later ejected is skipped with a message instead of
publishing a release for a commit that never landed. It waits for `main` to
catch up before deciding that, because the dispatch fires when the
`merge_group` run completes and the queue fast-forwards `main` a moment
later. A failure to fetch the ancestry is a hard error, not a silent skip
(#4930).
