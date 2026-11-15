## Support `--from-archive` via shared `RunSource` abstraction

- Added `RunSource`, `LiveRunSource`, and `ArchiveRunSource` in
  `bernstein.core.observability.run_source`, plus fail-closed extraction
  `open_verified_archive_workdir` and `resolve_run_source` in
  `bernstein.cli.run_archive`.
- Extended `ARCHIVE_SECTIONS` in `bernstein.cli.run_archive` to include
  `.sdd/runs/*/summary.json` (`tasks`), `.sdd/logs/*.log` (`logs`), and
  `.sdd/metrics/task_*.json` (`metrics`) so archived postmortem inputs match
  live workspaces.
- Added `--from-archive PATH` to `bernstein report postmortem` and
  `bernstein insights` (`--workdir` + `--from-archive`), refusing tampered or
  unverifiable bundles fail-closed. Refs #5443.
