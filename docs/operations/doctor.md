# bernstein doctor

The default doctor's checks also produce installation audit findings:

```sh
bernstein doctor --json
bernstein govern audit --only INS --json
bernstein govern audit --list --only INS
```

The doctor's JSON retains `checks` and `runtime` and adds `findings`. That array
uses exactly the same schema and producers as the installation audit's JSON
array. The text report, display order, readiness calculation, exit behavior and
`--fix` workflow remain unchanged. Use `bernstein govern audit --only INS` to select the installation checks
and measure the host.
The audit CLI explicitly composes the installation catalog into its runner;
the core default registry remains independent of CLI doctor implementations.
See [installation check composition](../sdd/installation-check-composition.md)
for the ownership boundary and its regression checks.

Each producer has a stable ID, even when it emits several display rows or has
no configured subject:

| ID | Installation check |
| --- | --- |
| INS-001 | Python version |
| INS-002 | Adapter binaries |
| INS-003 | Adapter last-green advisories |
| INS-004 | Adapter version posture |
| INS-005 | Adapter authentication |
| INS-006 | Server port |
| INS-007 | Workspace structure |
| INS-008 | Stale PID files |
| INS-009 | Guardrail statistics |
| INS-010 | CI tools |
| INS-011 | Storage backend (including PostgreSQL and Redis variants) |
| INS-012 | Secrets manager |
| INS-013 | Readiness |
| INS-014 | Context files, MCP, permissions, installations and plugins |
| INS-015 | Commit attribution |
| INS-016 | Compliance prerequisites |
| INS-017 | Schedule supervisor |
| INS-018 | Eval gate power advisory |
| INS-019 | OTLP export advisory |
| INS-020 | Audit anchoring |

Audit findings use `pass`, `fail`, or `not_measurable`. A tool probe that cannot
run is `not_measurable` with the tool and prerequisite named; it never becomes
a passing finding. Checks without observations are also `not_measurable`.
For INS-005, a configured Codex model cannot make a missing `codex` login
probe measurable; an observed API-key setting does not require that CLI probe.
For INS-004, any `unknown_version` entry is `not_measurable` and names the
adapter's unavailable `<adapter> --version` probe. Measured versions still
pass at or above the floor and fail below it.
These audit verdicts preserve the legacy doctor's advisory display and exit
behavior. Version-posture evidence hashes the observed versions and anchoring
state, excluding the receipt timestamp so repeated observations compare equal.

`bernstein doctor` is the single-command checklist for diagnosing a
broken Bernstein installation before opening an issue. The legacy
`bernstein doctor` runs the same checks it always did; the new
`bernstein doctor extended` subcommand layers on three additional
categories: adapter binaries, network reachability, and CI/sandbox
context.

## Quick start

```bash
bernstein doctor                                      # legacy report
bernstein doctor --suggest-docs                       # top curated documentation gaps
bernstein doctor extended                             # full extended report
bernstein doctor extended --json                      # machine-readable output
bernstein doctor extended --adapter claude --provider anthropic
BERNSTEIN_OFFLINE=1 bernstein doctor extended         # skip every network probe
```

Exit code: `1` if any check fails, `0` otherwise. Warnings do not
trigger a non-zero exit but are echoed to stderr.

## Documentation gap hints

`bernstein doctor --suggest-docs` reads a small operator-curated list
of common in-CLI documentation gaps and prints the top entries so
operators can navigate to (or request) the closest matching page. The
default doctor run ends with a one-line hint pointing at the flag.

The list ships in the wheel at
`bernstein._default_templates/docs/_unanswered.json` and is refreshed
by the maintainer on each release as part of release preparation.

Schema (one entry per object):

| Field               | Type   | Purpose                                               |
|---------------------|--------|-------------------------------------------------------|
| `topic`             | string | Short label of the gap                                |
| `related_command`   | string | Closest existing CLI command for navigation           |
| `doc_page_proposed` | string | Repo-relative path for the proposed new page          |
| `source`            | string | Provenance tag, e.g. `operator-curated-YYYY-MM-DD`    |
| `count`             | int    | Weight; higher counts surface first                   |

Malformed or missing files degrade to a friendly "no gaps recorded"
note so the flag never crashes the diagnostic surface.

## Check categories

| Category        | Source module                                         | Honors                |
|-----------------|-------------------------------------------------------|-----------------------|
| `installation`  | `bernstein.cli.install_check` (preserved unchanged)   | n/a                   |
| `adapter`       | `bernstein.cli.doctor.adapter_checks`                 | `bernstein.yaml`      |
| `network`       | `bernstein.cli.doctor.network_checks`                 | `BERNSTEIN_OFFLINE=1` |
| `environment`   | `bernstein.cli.doctor.environment_checks`             | env vars + file probes|
| `environment`   | `bernstein.cli.doctor.audit_lock_checks`              | audit dir filesystem  |

### Installation checks

Unchanged from before:

- duplicate `bernstein` binaries on `PATH`
- installed package version
- virtual-environment isolation

### Adapter binary checks

For each adapter declared in `bernstein.yaml`:

1. resolve the binary via `shutil.which`
2. spawn `<binary> --version` with a 5-second timeout
3. report PATH presence, version string, exit code, or hang

Status mapping:

| Outcome                        | Status |
|--------------------------------|--------|
| binary present, version capture| `ok`   |
| `--version` hangs              | `warn` |
| `--version` exits non-zero     | `warn` |
| binary not on `PATH`           | `fail` |

### Network reachability

Each provider maps to a single hostname. The check opens a TCP
connection to port 443 with a 2-second timeout.

| Provider     | Host                                       |
|--------------|--------------------------------------------|
| anthropic    | `api.anthropic.com`                        |
| openai       | `api.openai.com`                           |
| google       | `generativelanguage.googleapis.com`        |
| openrouter   | `openrouter.ai`                            |
| groq         | `api.groq.com`                             |
| mistral      | `api.mistral.ai`                           |
| deepseek     | `api.deepseek.com`                         |

Set `BERNSTEIN_OFFLINE=1` to skip every network probe. The doctor
report shows a single compact `network:*` skip row in that case.

DNS-resolution failures, refused connections, and timeouts are reported
distinctly so the operator can tell a broken resolver from blocked
egress.

### Environment / sandbox detection

Detection combines environment variables with light-weight file
probes. Detected environments are surfaced in the report so issue
reporters can include the context automatically.

| Marker                        | Source                              |
|-------------------------------|-------------------------------------|
| `GITHUB_ACTIONS=true`         | GitHub Actions                      |
| `GITLAB_CI=true`              | GitLab CI                           |
| `BUILDKITE=true`              | Buildkite                           |
| `CIRCLECI=true`               | CircleCI                            |
| `JENKINS_URL`                 | Jenkins                             |
| `DEVCONTAINER=true`           | VS Code devcontainer                |
| `REMOTE_CONTAINERS=true`      | VS Code devcontainer (remote)       |
| `INVOCATION_ID`               | systemd-run                         |
| `/.dockerenv` exists          | Docker                              |
| `CI=true` (no specific match) | Generic CI                          |

Multiple markers can match simultaneously (for example, Docker inside
GitHub Actions). The renderer suppresses the generic `CI` row when a
more specific environment matched.

### Audit-chain append lock

`audit:chain-lock-filesystem` reports whether the audit directory sits where the
chain's cross-process append lock actually works. Appends to
`.sdd/audit/<day>.jsonl` serialise under `flock(LOCK_EX)` on one `.chain.lock`
per directory; that is what stops two processes recovering the same chain tail
and each appending a record carrying the same stale `prev_hmac`.

| Result | When |
|--------|------|
| `ok`   | local filesystem (ext4, xfs, btrfs, APFS, ZFS) or any type not on the advisory list |
| `warn` | NFS, SMB/CIFS, 9p, virtiofs, FUSE passthrough, or an overlay upper layer |
| `warn` | no `fcntl` on this platform (Windows): the append section is a no-op |
| `skip` | the filesystem type could not be determined |

The type is read from `/proc/self/mountinfo` on Linux and from `mount` on
macOS/BSD, taking the longest mount point that covers the directory. The check
never returns `fail`: an operator running on a share has a working install and a
deployment decision to make, and a red `doctor` would train them to ignore it.

Remediation when it warns: keep the audit directory on a local filesystem, or
give each host its own audit directory and reconcile the chains offline. See
`docs/cluster/deployment-patterns.md`.

## Example output

```
                    Bernstein Doctor
─────────────────────────────────────────────────────────────────
 Check               Category      Status   Detail                Remediation
 install:bernstein   installation  ✓ OK     v3.5.0
 adapter:claude      adapter       ✓ OK     /usr/local/bin/claude -> claude-code 2.1.5
 adapter:codex       adapter       ✗ FAIL   Binary `codex` not in PATH   Install via vendor instructions
 network:anthropic   network       ✓ OK     reachable: api.anthropic.com:443
 network:openai      network       ✓ OK     reachable: api.openai.com:443
 env:github-actions  environment   ✓ OK     GitHub Actions detected
─────────────────────────────────────────────────────────────────
 4 OK   0 WARN   1 FAIL   0 SKIP
```

## Programmatic API

```python
import asyncio
from bernstein.cli.doctor import run_all, render_report, exit_code_for

results = asyncio.run(run_all())
render_report(results)
raise SystemExit(exit_code_for(results))
```

Every result is a frozen `DoctorResult(name, category, status, detail,
remediation)` dataclass. `category` is one of `installation`,
`adapter`, `network`, `environment`. `status` is one of `ok`, `warn`,
`fail`, `skip`.

## Troubleshooting matrix

| Symptom                                         | Likely fix                                                   |
|-------------------------------------------------|--------------------------------------------------------------|
| `adapter:* fail - Binary not in PATH`           | install the vendor CLI or remove the adapter from config     |
| `adapter:* warn - exited 2`                     | upgrade the binary; check `<bin> --version` manually         |
| `adapter:* warn - timed out after 5s`           | the binary is wedged; kill stale processes                   |
| `network:* fail - DNS lookup failed`            | fix `/etc/resolv.conf` or your DNS resolver                  |
| `network:* fail - connection refused`           | check proxy or firewall egress rules                         |
| `network:* skip - BERNSTEIN_OFFLINE=1`          | expected when air-gapped; clear the env var to re-enable     |
| `env:docker` with no other CI markers           | host is a vanilla Docker container; include in bug reports   |

## Provider failover drill

`bernstein doctor --failover-drill` exercises every fallback chain declared
under `provider_availability` in `bernstein.yaml` and exits non-zero when
any declared chain element is broken. See
[Provider availability & failover](provider-availability.md).

## Aged leaked lanes (#6572)

The default `bernstein doctor` report checks confirmed leaked worktrees and
sandbox sessions using the same canonical TaskStore replay snapshot. A lane
is considered only when the sweep confirms all recorded owning tasks are
finished; missing ownership, unknown tasks, active/resumable owners and
superseded sandbox lifetimes remain excluded. The worktree and sandbox
prerequisites landed through [#6919](https://github.com/sipyourdrink-ltd/bernstein/pull/6919)
and [#6733](https://github.com/sipyourdrink-ltd/bernstein/pull/6733).

`BERNSTEIN_LEAK_MAX_AGE_SECONDS` sets the threshold in seconds, defaulting to
`86400` (24 hours). The internal check's explicit `max_age_seconds` argument
has precedence over the environment. Values must be finite and non-negative;
invalid configuration is an unavailable-check error. The comparison is
strictly **older than**: a lane exactly at the threshold is not aged.

Age describes the resource lifetime, **not time since task completion or
leak onset**. Worktrees use directory birthtime when available, otherwise
filesystem ctime (`directory_birthtime_or_ctime`); sandboxes use their audited
session-create time (`audit_session_create`). A confirmed leak with missing,
negative or non-finite age receives an unknown-age warning rather than a
clean result. The existing classifiers' clock-skew handling is unchanged.

| Evidence | Check result | Default doctor exit contribution |
| --- | --- | --- |
| Confirmed leak older than threshold | WARN with lane ID, owner IDs/statuses, age and source | Non-blocking |
| Confirmed leak with unusable age | WARN with unknown age | Non-blocking |
| No aged or unknown-age confirmed leaks | PASS | Non-blocking |
| Unreadable/invalid task or audit evidence, or invalid threshold | FAIL with unavailable-check detail | Exit 1 |

The existing aggregate text row retains each lane's details. In
`bernstein doctor --json`, that row in `checks` includes `leaked_lanes`, a
list of structured records with `kind`, `resource_id`, `task_ids`,
`task_statuses`, `age_seconds`, `age_source`, `max_age_seconds` and `reason`.
`reason` is `age_exceeded` or `age_unavailable`; unavailable ages are JSON
`null`, never NaN. An unavailable check emits no asserted lane findings.
These records are separate from the installation audit's `findings` catalogue.

The check reads evidence only. It does not delete lanes, update tasks, append
audit events, mint keys or contact sandbox providers, and has no automatic
removal action under `--fix`. Preview remediation with `bernstein worktrees
sweep` or `bernstein sandbox sweep`. Applying a removal plan still requires
its separate authenticated removal gate; a doctor warning grants no removal
authority. See [worktree operations](worktrees.md#task-terminal-lane-sweep).

Regression coverage in `tests/unit/sandbox/test_leaked_lane_sweep.py` exercises
both kinds, canonical statuses, a reopen after the shared snapshot,
threshold/default/override boundaries, unavailable task evidence, unknown
ages, existing sweep exclusions and byte-preserving reads. The public CLI
JSON/text and error-exit boundary is exercised by
`tests/unit/test_doctor_leaked_lanes.py`.
