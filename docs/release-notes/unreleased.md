# Unreleased

Changes merged to `main` that are not yet part of a tagged release. Each
tagged release has its own page in this directory; this page carries what has
landed since the newest one.

Cutting a version empties this page: every entry the tag ships moves onto that
version's page in the release PR itself. `tests/unit/test_unreleased_notes_rotation.py`
holds the page to that — an entry naming an issue or PR a tagged release page
already documents fails the build. An entry that cites released work as context
rather than as its own attribution is exempted by hand there, with the reason.

## Audit

- `verify-audit-receipt` and `bernstein audit receipt verify` work from an installed package. Both run the standalone verifier `tools/verify_audit_receipt.py`, which the wheel did not carry, so every pip, uv, pipx or conda install exited 2 with "can't open file". The wheel now bundles the script, both commands fall back to that copy outside a source checkout, and the install smoke jobs run `verify-audit-receipt --help`.

## Cluster

- `bernstein live` pointed at a cluster central shows the worker nodes. The AGENTS panel read only this machine's `.sdd/runtime/agents.json`, so against a remote central it stayed on "Initializing orchestrator..." while the TASKS panel filled up. When the server URL is not loopback, or a local server has registered nodes, the panel now lists each node (status, adapter, model label, claimed, slots, heartbeat age) with the tasks it holds under it. `GET /tasks` responses carry `claimed_by_session` additively so tasks can be placed under the node that claimed them. A remote view does not read local runtime files, disables hot restart, and quits on `q` without draining.

## Eval

- Incident eval cases synthesised from dead-lettered tasks and post-mortems no longer land in the source tree of the project being orchestrated. Outside Bernstein's own source tree they are written to `.sdd/eval/cases/incidents/`, with the rest of the run state the orchestrator never stages; Bernstein's source tree keeps extending its checked-in `src/bernstein/eval/cases/incidents/`. The P0 incident gate and `bernstein audit diagnose --signal incident:<case-id>` read the same directory the synthesiser writes, an explicit `cases_dir` still overrides it, and `bernstein eval sync-incidents` prints where it wrote. A `src/bernstein/eval/cases/incidents/` directory an earlier version left in another project is no longer read or extended: delete it, or move its files into `.sdd/eval/cases/incidents/`.

## Release

- Copr RPM publishing runs in its own `copr` deployment environment, so a Copr build failure no longer marks the PyPI deployment as failed.
