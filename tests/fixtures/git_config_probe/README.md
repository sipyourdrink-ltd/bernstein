# git_config_probe

Fixture for `tests/unit/test_repo_git_config_probe.py`.

`probe.sh` records one line per invocation to `git-config-probe.log` in
`TMPDIR` and `HOME`. The scripts under `hooks/` and `filter.sh` delegate to it,
so any hook slot or the `probe` filter driver leaves the same record.

The test points `core.hooksPath`, `core.fsmonitor` and `filter.probe.*` at these
scripts on the repository it runs in. `sample.probe` is bound to the filter by
`.gitattributes`.
