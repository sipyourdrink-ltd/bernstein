# bernstein doctor

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

## Proposed aged-leak finding (#6572)

**Design proposal only: unimplemented.**
[Issue #6572](https://github.com/sipyourdrink-ltd/bernstein/issues/6572)
requires both worktree and sandbox listing prerequisites before a doctor
finding. [#6733](https://github.com/sipyourdrink-ltd/bernstein/pull/6733)
continues the sandbox half of #6549; it explicitly leaves doctor separate.
The worktree contribution
[#5751](https://github.com/sipyourdrink-ltd/bernstein/pull/5751) remains a
draft with unresolved persistence/evidence and public listing boundaries.
This proposal preserves that contributor's work and does not modify the PR
or claim either prerequisite supplies the complete doctor feature.

The accepted goal is a read-only finding for a **confirmed leaked** worktree
or sandbox older than an agreed configurable age, naming the lane, owning
task IDs, recorded statuses and age. Only the prerequisite sweeps' leaked
verdicts count: old active/resumable lanes, missing ownership/task records,
mixed active owners and superseded sandbox lifetimes remain excluded.
Unreadable task/audit evidence must be a doctor error, never a clean result.
The check must not remove a lane, change a task, append audit events or mint
a key. Advisory records do not authorize removal.

### Unresolved boundaries

1. **Public threshold configuration.** Decide the configuration source,
   units, documented default, override precedence and invalid-value behavior.
   The issue explicitly leaves the default undecided; no new flag, env name
   or seed schema is selected here.
2. **Age evidence and comparison.** Decide the age source, threshold
   comparison and unknown/unusable timestamp behavior. Existing sandbox ages
   use create-event time; worktree ages use filesystem ctime. Neither proves
   time since task completion or leak onset. Trace-staleness thresholds are
   separate and do not supply an age default for this finding.
3. **Diagnostic surface and result contract.** Decide the normal/extended
   doctor integration, per-lane or aggregate text/JSON output, finding
   severity/exit behavior and distinct evidence-unavailable errors. No
   severity, unknown-age result or public output schema is selected here.
4. **Dependency readiness.** Land the corrected worktree sweep/evidence
   boundary from #5751 alongside the sandbox half before integration. Do not
   invent a second status reader, ship sandbox-only coverage or infer a
   removal policy from the existing advisory listing.

### Future acceptance matrix

These are proposed properties, not implemented or executed tests. The
threshold and reporting decisions above remain open.

1. `test_doctor_reports_both_confirmed_leak_kinds_older_than_configured_age`:
   real task/archive/audit/worktree fixtures produce both finding kinds with
   exact lane IDs, owning task IDs/statuses and honest age provenance.
2. `test_doctor_age_threshold_preserves_younger_and_boundary_candidates`:
   an injected clock covers below/at/above the agreed threshold without a
   hardcoded default or trace-staleness shortcut.
3. `test_old_active_or_excluded_lanes_never_become_leak_findings`: old active,
   resumable, orphaned, missing-owner/task, mixed-owner and superseded cases
   remain excluded regardless of PID liveness or filesystem age.
4. `test_doctor_refuses_incomplete_task_or_audit_evidence_without_clean_report`:
   unreadable, invalid, torn or replay-skipped evidence returns the agreed
   error with its source path instead of replaying an older terminal result.
5. `test_doctor_leak_status_matches_native_archive_and_live_reopen`: actual
   TaskStore archive IDs and reopen precedence, every agreed finished status
   and live controls agree across both sweeps and doctor.
6. `test_doctor_age_reports_unknown_and_future_time_honestly`: missing or
   invalid timestamps and clock skew follow the agreed contract without
   inventing age from unrelated metadata or completion prose.
7. `test_doctor_leak_age_configuration_is_explicit_and_validated`: cover the
   approved source/default/units/precedence, invalid values and project
   isolation without widening unrelated configuration.
8. `test_doctor_leak_check_never_mutates_project_or_removes_lanes`: complete
   byte snapshots and forbidden-write controls prove no removal, task
   mutation, key creation or audit append, including under `--fix`.
9. `test_doctor_leak_findings_reach_agreed_text_json_and_exit_surface`:
   exercise the approved CLI collector/renderer with distinct agreed results
   for confirmed old leaks and evidence errors; general readiness must not
   misrepresent this finding.
