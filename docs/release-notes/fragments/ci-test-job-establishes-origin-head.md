## The test shards establish `origin/HEAD`, so the AGENTS.md mirror guard stops flaking

`tests/unit/test_agents_md_mirror_guards.py` resolves the repository's default
branch through `refs/remotes/origin/HEAD`. `actions/checkout` does not set that
symbolic ref, which is why `repo-hygiene` runs `git remote set-head origin -a`
right after its checkout and says so in a comment.

The `test` and `test-macos` shard jobs never did. They check out shallow and
fetch only the pull request's base commit into `origin/pr-base`, so the guard
raised `DefaultBranchUnresolvedError` — but only on the runs where the
affected-test selector happened to pull that file into a shard's slice. A
failure that appears on some pull requests and not others, in a file the pull
request never touched, reads as an unrelated flake and was re-run as one.

Both jobs now run the same step, and
`tests/unit/scripts/test_ci_establishes_origin_head.py` makes it a property of
any job that selects tests from the diff rather than something each job has to
remember. It is deliberately narrower than "runs pytest": `beartype` runs
`pytest tests/unit/` but narrows to three lineage files with `-k`, and
`integration-tests` points the runner at `tests/integration`, so neither can
select the guard.
