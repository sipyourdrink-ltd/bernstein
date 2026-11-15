# CI fix for PR #7644 / issue #6976

Verdict: branch-caused failures, fixed locally.

## Cause and evidence

Read-only inspection used `gh pr checks 7644 -R sipyourdrink-ltd/bernstein`
and `gh run view 43626132035 -R sipyourdrink-ltd/bernstein --log-failed`.
The branch commit inspected was `fd2130f06`; CI tested merge commit
`3cc56d8b93d2fce8568b361b53ff71c5491f34c8` against base
`0c94b12e38bc29ae681d0ea4a4cb2fc290b25cbd`.

- [Linux Python 3.13 shard 7](https://github.com/sipyourdrink-ltd/bernstein/actions/runs/43626132035/job/132542536300)
  failed `test_no_readme_link_depends_on_the_repository_around_it` in
  `tests/unit/test_packaged_readme_renders_off_repo.py`. The new README
  paragraph introduced relative destinations `templates/adapter_plugin` and
  `docs/operations/adapters.md#building-an-adapter-from-the-template`, which
  cannot resolve when PyPI renders the packaged README.
- [Repo hygiene](https://github.com/sipyourdrink-ltd/bernstein/actions/runs/43626132035/job/132542220669)
  failed `bernstein readme-l10n verify --workdir .`: that same paragraph
  changed the "supported agents" section hash from `sha256:45abeaa3624d` to
  `sha256:933255771a3f` and its paragraph count from two to three. All 23
  translations retained the previous content: 46 drift findings.
- [CI gate](https://github.com/sipyourdrink-ltd/bernstein/actions/runs/43626132035/job/132543964715)
  failed because `repo-hygiene` and `test` failed; it adds no separate cause.

Both errors reproduce locally and originate in the branch's README addition.
They are not classified as CI-NOT-OURS.

## Change

Remove the optional adapter-template announcement paragraph from README.md.
This restores the existing translation contract and removes both relative
links with a two-line deletion. The detailed template documentation added by
the PR in `docs/operations/adapters.md` and the adapter implementation/tests
remain intact. No gate, assertion, translation binding, or workflow is relaxed.

## Test results

Commands used the existing virtualenv (`UV_NO_SYNC=1`), with `UV_CACHE_DIR`
and `TMPDIR` under this checkout's `.cache/cifix/`. Pytest used a checkout-local
`--basetemp`. Local Python is 3.14; the failing CI test job used Python 3.13.

Before the change:

- `uv run pytest -q tests/unit/test_packaged_readme_renders_off_repo.py`:
  **1 failed, 7 passed**, with the same two relative links as CI.
- `uv run bernstein readme-l10n verify --workdir .`:
  **46 drift findings across 23 languages**, matching CI.

After the change:

```sh
uv run pytest -q tests/unit/test_packaged_readme_renders_off_repo.py tests/unit/test_readme_l10n_cmd.py tests/unit/test_adapter_plugin_template.py
```

**56 passed in 2.32s**, including generated adapter tests and fake-CLI launches.

- `uv run bernstein readme-l10n verify --workdir .`: **exit 0**, all 23
  translated READMEs in sync.
- `git diff --check`: **passed**.

## Local handoff

Only README.md and CIFIX.md belong to this follow-up commit. Existing
untracked ISSUE.md, PR.md, PRODUCER.txt, REPORT.md, and REVIEW.txt are preserved.
The requested checkout and branch remain for review; no push, PR comment,
label, remote CI rerun, merge, or teardown was performed. Remote checks still
describe the previously submitted commit until an authorized push occurs.
The referenced `team/RUNTIME-INSTRUCTIONS.md` is absent from this checkout.
