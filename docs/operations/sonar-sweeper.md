# Sonar findings to code scanning

Routes open SonarQube findings into GitHub code scanning (the Security
tab) as SARIF. On a public repository this keeps finding detail in a
surface that only maintainers (write access and above) can read, instead
of on the public issue tracker.

## TL;DR

| What | Where |
|------|-------|
| Emitter | `scripts/sweep_sonar_findings.py --emit-sarif sonar.sarif` |
| Workflow | `.github/workflows/sonar-code-scanning.yml` (daily cron + manual dispatch) |
| Upload | `github/codeql-action/upload-sarif` with `category: sonarqube` |
| Output | SARIF 2.1.0 uploaded to the Security tab; no files committed |
| Covers | issues of every type (BUG, VULNERABILITY, CODE_SMELL) and security hotspots |

## Env vars

| Env var | Purpose | Required? |
|---|---|---|
| `SONAR_HOST_URL` | Sonar server base URL. | yes (unless `--fixture` used) |
| `SONAR_TOKEN` | User token with `Browse` permission on the project. | yes (unless `--fixture` used) |
| `SONAR_PROJECT_KEY` | Project key. | optional (defaults to `bernstein`) |

These match the existing `bernstein doctor sonar` env contract. In CI the
workflow reads `SONAR_HOST_URL`/`SONAR_PROJECT_KEY` from repository
variables and `SONAR_TOKEN` from a repository secret.

## How it works

1. The emitter reuses the same Sonar client the sweeper uses: it pages
   `/api/issues/search` for issues of every type and severity and
   `/api/hotspots/search` for security hotspots.
2. Each finding becomes a SARIF result. The Sonar component key has its
   project prefix stripped (`bernstein:src/...` -> `src/...`) so the
   `artifactLocation.uri` is repo-relative and code scanning can anchor
   it to a line.
3. Each unique rule becomes a SARIF `rules[]` entry with a `helpUri`
   back to the Sonar rule page. Vulnerabilities and hotspots also carry a
   `security-severity` property (a 0-10 value mapped from the Sonar
   severity or hotspot probability) so GitHub buckets them correctly.
4. Every result carries `partialFingerprints.sonarFindingKey` (the Sonar
   issue key) so code scanning dedup is stable across runs.
5. The output is deterministic: rules and results are sorted on stable
   keys, so an unchanged finding set produces byte-identical SARIF.

The workflow uploads the SARIF via `github/codeql-action/upload-sarif`
under `category: sonarqube`. It never opens issues and never commits
files. A guard step fails the run clearly when `SONAR_HOST_URL` or
`SONAR_TOKEN` is missing, rather than uploading an empty document.

## Local invocation

```
# Emit SARIF from the live Sonar server.
SONAR_HOST_URL=... SONAR_TOKEN=... \
  uv run python scripts/sweep_sonar_findings.py --emit-sarif sonar.sarif

# Emit SARIF from a saved fixture (no network at all).
uv run python scripts/sweep_sonar_findings.py \
  --emit-sarif sonar.sarif \
  --fixture tests/unit/sweep/fixtures/issues_search.json

# Inspect the result.
python -c "import json; d=json.load(open('sonar.sarif')); \
  print('rules', len(d['runs'][0]['tool']['driver']['rules']), \
        'results', len(d['runs'][0]['results']))"
```

## Local backlog sweep (on demand)

The same script can still turn findings into local `.sdd/backlog/` ticket
files for on-demand triage via `bernstein doctor sonar-sweep`. That mode
writes gitignored files under `.sdd/` and is intended for local use; it
no longer runs on a schedule. See the command's `--help` for options.
The public-safe `## Why` bodies for that path are still synthesised from
the vetted rule-family blurb table in `scripts/sweep_sonar_findings.py`
(the raw Sonar message is never copied into a ticket); a unit test
asserts no blurb contains a string from `FORBIDDEN_SUBSTRINGS`.

## Tests

```
uv run pytest tests/unit/sweep/ -q
```

The suite covers the SARIF emitter (required 2.1.0 structure, project-key
prefix stripping, the VULNERABILITY -> error + `security-severity`
mapping, security-hotspot handling, and byte-for-byte determinism) plus
the local backlog sweep (de-dup, idempotent double-run, severity filter,
per-day cap, the rule-family blurb guard, exclusive-create emission, and
the HTTP retry on 5xx/429).
