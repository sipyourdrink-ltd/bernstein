# Extended static analysis

Additional static-analysis surface that complements the existing
ruff + mypy + bandit + CodeQL lane. Each tool catches a different
bug class; together they close gaps that single-tool runs miss.

Workflow: `.github/workflows/static-analysis-extended.yml`.

## TL;DR

| Job          | Tool      | Lane                     | Gate           | SARIF | Where findings show |
|--------------|-----------|--------------------------|----------------|-------|---------------------|
| `semgrep`    | Semgrep CE| push / merge_group / cron| Fail on new    | Yes   | Security tab        |
| `trivy-fs`   | Trivy     | push / merge_group / cron| Fail HIGH/CRIT | Yes   | Security tab        |
| `trivy-iac`  | Trivy     | push / merge_group / cron| Fail HIGH/CRIT | Yes   | Security tab        |
| `vulture`    | vulture   | weekly cron only         | Advisory       | Yes   | Security tab        |
| `refurb`     | refurb    | weekly cron only         | Advisory       | Yes (error-level only) | Security tab |
| `perflint`   | pylint+perflint | weekly cron only   | Advisory       | Yes   | Security tab        |

All jobs run in parallel; total wall-clock is dominated by Semgrep
(under 5 minutes for the current `src/` tree). The advisory
style-and-hygiene jobs (vulture, refurb, perflint) run on the weekly
schedule and `workflow_dispatch` only, so merge-queue runs stay lean
(issue #2764).

## What each tool catches

| Tool      | Bug class                                                    |
|-----------|--------------------------------------------------------------|
| Semgrep CE| Pattern-based Python issues that CodeQL free tier skips      |
| Trivy fs  | CVEs in lockfile deps + leaked secrets in tracked files      |
| Trivy IaC | Dockerfile / docker-compose / helm / kustomize misconfigs    |
| vulture   | Unused functions / classes / vars across the 40-adapter tree |
| refurb    | Outdated Python idioms with cleaner modern equivalents       |
| perflint  | Hot-path antipatterns (string concat in loops, etc.)         |

This stack does not replace ruff / mypy / bandit / CodeQL; it adds
the surface those tools do not cover.

## Triggers

- `push` to `main` (path-filtered to source / config / IaC / workflow):
  security-signal jobs only.
- `merge_group` (merge-queue ephemeral branch): security-signal jobs only.
- Weekly cron Sunday 05:23 UTC: all jobs, including the advisory
  vulture / refurb / perflint lane.
- `workflow_dispatch` for ad-hoc runs: all jobs.

## Baseline policy

### Semgrep

`.semgrep/baseline.yml` records pre-existing findings on `main` for
transparency and audit. The actual gate is git-baseline based:
`semgrep scan --baseline-commit=<base-sha>` on pull requests so only
new findings introduced by the PR fail the job.

To regenerate the snapshot file:

```
uv tool run semgrep scan \
    --config p/python \
    --config p/security-audit \
    --severity ERROR --severity WARNING \
    --json --metrics off --quiet src/ \
    > /tmp/semgrep.json
```

Then either update `.semgrep/baseline.yml` by hand or write a small
script to convert the JSON.

### Trivy

No baseline. HIGH / CRITICAL findings fail the job immediately;
findings below that threshold still surface in the Security tab.

### vulture / refurb / perflint

No baseline; jobs are advisory and run on the weekly schedule (plus
`workflow_dispatch`) only. SARIF uploads still happen so findings
unify in Code Scanning, with one exception: the refurb upload keeps
error-level results only, so its style findings do not create
code-scanning alerts that bury real security findings (issue #2764).
The full refurb text output stays available as a workflow artifact.
Promote a job to a hard gate by removing the trailing `|| true` in
the relevant step once the existing backlog drops to zero.

## SARIF conversion

vulture, refurb, and perflint do not emit SARIF natively. The
workflow pipes their text output through `scripts/text_to_sarif.py`
which produces a SARIF 2.1.0 log suitable for
`github/codeql-action/upload-sarif`.

Adding a new tool is a matter of adding a regex + tool-meta entry in
`scripts/text_to_sarif.py` and a job block that mirrors one of the
advisory jobs.

## Local reproduction

```
# Semgrep CE
uv tool run semgrep scan --config p/python --config p/security-audit \
    --severity ERROR --severity WARNING --metrics off src/

# Trivy filesystem
trivy fs --severity HIGH,CRITICAL --ignore-unfixed .

# Trivy IaC
# .clusterfuzzlite/ is skipped: the fuzzing harness inherits the
# OSS-Fuzz base-builder image which runs as root by framework
# requirement and is not deployable infra.
trivy config --severity HIGH,CRITICAL --skip-dirs .clusterfuzzlite .

# vulture
uv tool run vulture src/ vulture_whitelist.py --min-confidence 70

# refurb
uv tool run refurb src/

# perflint (via pylint plugin)
uv tool run --from perflint pylint --load-plugins=perflint \
    --disable=all --enable=W8101,W8102,W8201,W8202,W8203,W8204,W8205,W8206,W8301 \
    src/
```

## Hardening

Every job runs `step-security/harden-runner` in audit mode and uses
SHA-pinned third-party actions. The workflow has no write
permissions beyond `security-events: write` for SARIF upload.
