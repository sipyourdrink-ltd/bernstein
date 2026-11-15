# Security Hardening Guide

How to lock down a Bernstein deployment for production use.

## Permission modes

Tool approval behavior depends on the selected adapter. For supported local
pre-approval settings, see [Always-allow rules](../operations/CONFIG.md#always-allow-rules-bernsteinalways_allowyaml).
For isolation and command execution boundaries, see the [sandbox guide](../architecture/sandbox.md).

## Permission rule engine

Use the controls documented for your adapter and deployment. The file-diff
checks in [Role-based file permissions](#role-based-file-permissions) are
separate from approval of individual tool calls.

## Policy-as-code engine

Run `bernstein policy check` from the project directory to evaluate the committed branch diff
against `main` using YAML or Rego policies in `.sdd/policies/`. For CI, fetch the intended base into a local `main` ref before running
the command.

### YAML policies

Each YAML file can contain one or more policy rules. A rule has a name, a severity
(`block` or `warn`), and a rule expression.

```yaml
# .sdd/policies/no-secrets.yaml
policies:
  - name: no-aws-keys
    severity: block
    rule: "diff_text !~ /AKIA[0-9A-Z]{16}/"

  - name: no-private-keys
    severity: block
    rule: "file_content !~ /-----BEGIN (RSA|EC|OPENSSH) PRIVATE KEY-----/"

  - name: no-env-files
    severity: block
    rule: 'file_path !~ /(?m)\.env$/'
```

**Rule expression syntax:**

| Form | Description |
|------|-------------|
| `field =~ /pattern/` | Requires the field to match the regex (violation if it does *not*) |
| `field !~ /pattern/` | Requires the field to *not* match the regex (violation if it does) |
| `field == value` | Requires exact equality |
| `field != value` | Requires inequality |
| `field > value` | Numeric comparison |

**Available fields:**

| Field | Type | Description |
|-------|------|-------------|
| `file_content` | string | Concatenated content of all changed files |
| `file_path` | string | Newline-separated list of changed file paths |
| `diff_text` | string | Full git diff of the agent's changes |
| `task_title` | string | Title of the task being evaluated |
| `task_description` | string | Description of the task being evaluated |
| `task_role` | string | Role assigned to the task (`backend`, `qa`, etc.) |
| `files_changed` | integer | Number of files changed in the diff |

The command prints violations. A `block` violation causes a non-zero exit.
A `warn` violation alone leaves the exit status successful.

### Rego policies (OPA)

For more expressive policies, write Rego rules and place them in `.sdd/policies/`.
Install `opa` on `$PATH` before using Rego in CI and validate that OPA can
load your policy. Bernstein skips Rego evaluation when OPA is absent and
logs OPA execution errors without reporting a policy violation.

```rego
# .sdd/policies/test-coverage.rego
package bernstein

import rego.v1

some_source_changed if {
    some path in input.file_paths
    startswith(path, "src/")
}

some_test_changed if {
    some path in input.file_paths
    startswith(path, "tests/")
}

deny contains "Source changed without a corresponding test change" if {
    some_source_changed
    not some_test_changed
}
```

The Rego input contains `task` (`id`, `title`, `description`, `role`),
`file_paths` (an array of changed paths), `files_changed`, `diff_text`, and
`file_content`.

Install OPA:

```bash
# macOS
brew install opa

# Linux
curl -L -o /usr/local/bin/opa https://openpolicyagent.org/downloads/latest/opa_linux_amd64_static
chmod 755 /usr/local/bin/opa
```

### View policy violations

```bash
bernstein policy check
```

## Role-based file permissions

Each agent role has a built-in permission matrix defining which paths it may modify.
Denied paths always override allowed paths.

### Default role matrix

| Role        | Allowed paths                                               | Denied paths                             |
|-------------|-------------------------------------------------------------|------------------------------------------|
| `backend`   | `src/*`, `tests/*`, `docs/*`, `pyproject.toml`, `scripts/*` | `.github/*`, `.sdd/*`, `templates/roles/*` |
| `frontend`  | `src/*`, `tests/*`, `docs/*`, `public/*`, `static/*`, `package.json` | `.github/*`, `.sdd/*`, `templates/roles/*` |
| `qa`        | `tests/*`, `src/*`, `docs/*`, `scripts/*`                   | `.github/*`, `.sdd/*`, `templates/roles/*` |
| `security`  | `src/*`, `tests/*`, `.github/workflows/*`, `docs/*`, `scripts/*` | `.sdd/*`, `templates/roles/*`          |
| `devops`    | `.github/*`, `Dockerfile`, `docker-compose.yaml`, `docker-compose.yml`, `scripts/*`, `Makefile` | `.sdd/*`, `src/*`, `templates/roles/*` |
| `docs`      | `docs/*`, `README.md`, `CHANGELOG.md`, `CONTRIBUTING.md`   | `.github/*`, `.sdd/*`, `src/*`, `tests/*`, `templates/roles/*` |
| `manager`   | `docs/*`, `.sdd/backlog/*`, `plans/*`                       | `src/*`, `tests/*`, `.github/*`          |
| `architect` | `src/*`, `tests/*`, `docs/*`, `scripts/*`                   | `.github/*`, `.sdd/*`, `templates/roles/*` |

Path traversal is enforced at the filesystem level: paths containing `..`, null bytes,
URL-encoded traversal sequences (`%2e%2e`, `%2f`), or symlinks that escape the project root
are blocked unconditionally, regardless of role permissions.

### Override role permissions

Programmatic guardrail callers can supply a role override map via
`GuardrailConfig.permission_overrides`. The seed config uses
[`role_model_policy`](../operations/CONFIG.md#role_model_policy-per-role-agent-configuration)
for model routing.

## Sandbox setup

### File system restrictions

Choose a [sandbox backend](../architecture/sandbox.md) with the filesystem
boundary required for the deployment. A worktree provides a separate working
directory. A sandbox backend supplies the execution boundary.

### Command restrictions

Use the selected adapter's tool approval settings and the sandbox backend's
execution controls. To check the resulting Git diff, run the
[policy check](#policy-as-code-engine).

### Git worktree isolation

By default, each coding agent runs in an isolated git worktree under `.sdd/worktrees/`. This means:
- Agents cannot access each other's in-progress changes
- A failing agent cannot corrupt the main branch
- Merge to main only happens after the janitor verifies the output

An artifact-mode task - one that completes on a signed lineage receipt
rather than a commit - runs in an isolated plain directory under
`.sdd/workspaces/` instead: a per-session working directory with no
checkout and no agent branch, so there is nothing to merge back. That
directory provides the same thing the worktree it replaces provides:
working-directory separation between agents, not kernel-level isolation.
The sandbox and network enforcement layers described elsewhere in this
document are unchanged and orthogonal to a task's output mode.

To inspect an agent's worktree before merge:

```bash
bernstein diff <task-id>    # Show the agent's git diff
bernstein trace <task-id>   # Show decision trace
```

## Audit mode

### Enable the audit log

Enable audit mode with `bernstein run --audit` or `BERNSTEIN_AUDIT=1`.
The audit log writes HMAC-chained records to `.sdd/audit/`. See the
[audit-log operator guide](audit-log.md) for record format and key management.

### Verify audit log integrity

```bash
# Verify the full HMAC chain and Merkle tree are intact
bernstein audit verify

# Inspect events from a specific date onward
bernstein audit query --since 2026-01-01 --limit 500
```

### Export to SIEM

Use the audit-log guide's [SIEM shipping instructions](audit-log.md#shipping-to-a-siem)
for supported export commands and log collection.

### Security pattern scanning

Bernstein automatically scans agent-produced diffs for security issues before merge.
The scanner runs without LLM calls, using static regex patterns:

- Hardcoded secrets (AWS keys, API tokens, passwords in source)
- Private key blocks (RSA, EC, DSA, OpenSSH)
- Unsafe `eval`/`exec` usage
- Shell injection risks (unsanitized inputs to subprocess)
- Weak cryptographic algorithms (MD5, SHA1 for security purposes)
- Path traversal patterns in code
- SQL injection vectors

Scan a task's diff for security findings:

```bash
bernstein security-review <task-id>
```

## Secret management

### Use environment variables, never config files

```bash
export BERNSTEIN_AUTH_JWT_SECRET="$(openssl rand -hex 32)"
export ANTHROPIC_API_KEY="sk-ant-..."
export OPENAI_API_KEY="sk-..."
export BERNSTEIN_OIDC_CLIENT_SECRET="..."
```

Set credentials through the environment or the deployment's secret manager.
See [API authentication](#api-authentication) for the supported variable names.

### Rotate secrets

Rotate the JWT signing secret and audit log HMAC key periodically:

```bash
# Generate a new JWT secret
export BERNSTEIN_AUTH_JWT_SECRET="$(openssl rand -hex 32)"
bernstein stop && bernstein run   # Restart to pick up new secret

# Rotate the audit log HMAC key. The key lives OUTSIDE .sdd/ by default.
# Default location: $XDG_STATE_HOME/bernstein/audit.key
#   (falls back to ~/.local/state/bernstein/audit.key)
# Override with: export BERNSTEIN_AUDIT_KEY_PATH=/secure/path/audit.key
KEY_PATH="${BERNSTEIN_AUDIT_KEY_PATH:-${XDG_STATE_HOME:-$HOME/.local/state}/bernstein/audit.key}"
cp "$KEY_PATH" "${KEY_PATH}.bak"
# Write a fresh HMAC key, then move pre-rotation chain files out of the
# active chain so verification does not fail on the old key.
openssl rand -hex 32 > "$KEY_PATH"
bernstein audit archive --corrupt --yes
```

### PII detection

Configure the [PII scan gate](pii-scan-gate.md) through `quality_gates.pii_scan`
and its related settings. The gate checks task output as part of verification.

## Network isolation

### Agent outbound access

Apply egress restrictions through the chosen [sandbox backend](../architecture/sandbox.md)
or the [sovereign profile](../compliance/eu-residency-customer-setup.md).
The task server's inbound IP allowlist is a separate setting.

### IP allowlisting for the task server

Restrict which IPs can reach the Bernstein task server:

```yaml
# bernstein.yaml
network:
  allowed_ips:
    - "10.0.0.0/8"          # Internal network
    - "172.16.0.0/12"       # VPN
    - "203.0.113.0/24"      # Office network
```

Localhost (127.0.0.1, ::1) is always allowed. Health check endpoints (`/healthz`) are
exempt from IP restrictions.

A range that does not parse is dropped with a warning, so the rest of the list still
applies. If *every* configured range is dropped the allowlist cannot be enforced at all,
and the server answers `500` on non-exempt paths rather than serving them unrestricted -
a typo in a CIDR must not silently turn the allowlist off. Health endpoints stay
reachable in that state so the container is not killed before the log line naming the bad
range can be read. An empty or omitted `allowed_ips` is unchanged: it means no
restriction was asked for.

## API authentication

### JWT authentication

Set `BERNSTEIN_AUTH_JWT_SECRET` and use `BERNSTEIN_AUTH_JWT_EXPIRY_SECONDS`
to configure the SSO JWT lifetime (default 86,400 seconds). See
[Security and Identity](../operations/security-and-identity.md) for human
sessions and [Manager Authentication](manager-auth.md) for agent identities
and the legacy bearer token.

### OIDC / SSO

Enable OIDC with `BERNSTEIN_OIDC_ENABLED=true`. Configure
`BERNSTEIN_OIDC_ISSUER_URL`, `BERNSTEIN_OIDC_CLIENT_ID`,
`BERNSTEIN_OIDC_CLIENT_SECRET`, and `BERNSTEIN_OIDC_REDIRECT_URI`. Set
`BERNSTEIN_OIDC_SCOPES` to a space-separated list when overriding the defaults.

### Cluster node authentication

The cluster seed accepts `cluster.auth_token`, a flat optional string.
See [Cluster mode](../operations/cluster-mode.md#jwt-node-authentication) for
worker credentials, token scopes, and revocation.

### Dashboard authentication

The server reads `BERNSTEIN_DASHBOARD_PASSWORD` for dashboard password login.
Dashboard sessions expire after 3,600 seconds by default.

### TLS termination

Bernstein does not terminate TLS directly. Use a reverse proxy:

```nginx
# nginx.conf
server {
    listen 443 ssl;
    server_name bernstein.yourcompany.com;

    ssl_certificate     /etc/ssl/certs/bernstein.crt;
    ssl_certificate_key /etc/ssl/private/bernstein.key;
    ssl_protocols       TLSv1.2 TLSv1.3;
    ssl_ciphers         HIGH:!aNULL:!MD5;

    location / {
        proxy_pass http://127.0.0.1:8052;
        proxy_set_header Host              $host;
        proxy_set_header X-Real-IP         $remote_addr;
        proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

### Rate limiting

Use named buckets under `rate_limit`. `paths` takes URL prefixes.

```yaml
rate_limit:
  task_requests:
    paths: [/tasks]
    requests_per_minute: 100
    window_seconds: 60
```

See [Security and Identity](../operations/security-and-identity.md#request-rate-limits)
for canonical and versioned routes.

## Compliance reporting

### Built-in compliance checks

Run the compliance audit against your current workspace:

```bash
bernstein govern audit-compliance
```

This command runs the compliance checks. See [Compliance profiles](../operations/compliance.md) for configuration and reporting.

### Generate a compliance report

```bash
# Structured JSON output for ingestion into compliance tooling
bernstein compliance report --json-output > compliance-$(date +%Y%m%d).json

# Human-readable summary
bernstein compliance report
```

The report covers:
- **Authentication**: auth method, token expiry, MFA presence
- **Audit**: log enabled, chain integrity, retention policy, SIEM export
- **Network**: isolation level, IP allowlist, TLS status
- **Data**: PII gate, encryption at rest, data residency policy
- **Dependencies**: vulnerability scan results, license compliance
- **Access control**: configured authentication and role coverage

### Dependency scanning

Run dependency and license scans through your project's dependency audit
tooling. Include the results in the compliance evidence for the deployment.

#### Scheduled scan scope

The orchestrator's scheduled (weekly) dependency scan is separate from the
compliance scan above and is **off by default**
(`OrchestratorConfig.dependency_scan_enabled = False`), so a scoped run is
never given unrequested remediation tasks. When it is enabled:

- It audits only the dependencies the target repo declares
  (`requirements*.txt`, `pyproject.toml`, `setup.py`/`setup.cfg`, or a
  lockfile). A repo with no Python dependency manifest is skipped rather than
  having the orchestrator's own tool environment audited.
- The first tick of a fresh run records a baseline instead of firing
  immediately, so the weekly interval starts from that baseline rather than
  treating a missing scan state as overdue.

### Data residency

See [EU Residency Customer Setup](../compliance/eu-residency-customer-setup.md)
for the `sovereign` profile and its placement and egress settings.

### Encryption at rest

Encrypt the `.sdd/` state directory:

```bash
# Linux: LUKS full-disk encryption
cryptsetup luksFormat /dev/sdb1
cryptsetup open /dev/sdb1 bernstein-state
mkfs.ext4 /dev/mapper/bernstein-state
mount /dev/mapper/bernstein-state /var/lib/bernstein

# macOS: encrypted APFS volume
diskutil apfs addVolume disk1 APFS bernstein-state -passphrase
```

## Production checklist

Before deploying Bernstein in a production environment:

**Authentication & access**
- [ ] API authentication enabled (`jwt`, `oidc`, or `bearer`)
- [ ] Dashboard password set
- [ ] TLS termination configured via reverse proxy
- [ ] IP allowlisting configured for task server
- [ ] Rate limiting enabled per endpoint
- [ ] Cluster node auth enabled (multi-node deployments)

**Agent sandboxing**
- [ ] Permission mode set to `auto` or stricter
- [ ] Role-based file permissions reviewed and tightened
- [ ] `.bernstein/rules.yaml` rules configured for your project
- [ ] Command policy (allow/deny lists) configured
- [ ] Network isolation level set (`restricted` minimum)

**Secrets & data**
- [ ] All secrets in environment variables, not config files
- [ ] JWT secret rotated (not default/empty)
- [ ] `.sdd/` directory on encrypted volume
- [ ] PII detection enabled
- [ ] Backup and restore tested

**Audit & compliance**
- [ ] Audit logging active and HMAC chain verified
- [ ] Audit log exported to SIEM
- [ ] Retention policy configured
- [ ] Dependency scanning enabled
- [ ] `bernstein govern audit-compliance` passes
- [ ] `bernstein compliance report` reviewed and archived
