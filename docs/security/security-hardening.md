# Security Hardening Guide

How to lock down a Bernstein deployment for production use.

## Permission modes

Bernstein enforces rules at four severity levels (critical, high, medium, low). The active
permission mode determines which levels are enforced:

| Mode      | critical | high | medium | low | Default for                      |
|-----------|:--------:|:----:|:------:|:---:|----------------------------------|
| `default` | ✓        | ✓    | ✓      | ✓   | Interactive CLI / TUI sessions   |
| `auto`    | ✓        | ✓    | ✓      | ✗   | Orchestrator (normal operation)  |
| `plan`    | ✓        | ✓    | ✗      | ✗   | Human-reviewed plan runs         |
| `bypass`  | ✓        | ✗    | ✗      | ✗   | Trusted CI / headless pipelines  |

✓ = rule enforced (deny/ask applies) · ✗ = rule relaxed (overridden to allow)

**Critical rules are always enforced, regardless of mode.** `bypass` does not disable
safety guardrails — it only relaxes medium and high severity rules for non-interactive runs.

Set the mode in config or via environment variable:

```yaml
# bernstein.yaml
permission_mode: auto   # bypass | plan | auto | default
```

```bash
export BERNSTEIN_PERMISSION_MODE=auto
```

When no rule matches a tool call, `default` mode falls back to `ask` (escalate to human).
All other modes fall back to `allow`.

**Legacy flag migration:** earlier Bernstein versions used `--dangerously-skip-permissions`.
That flag now maps to `bypass` mode — critical rules are still enforced.

## Permission rule engine

Rules are loaded from `.bernstein/rules.yaml` under the `permission_rules:` key. The first
matching rule wins. If no rule matches, the fallback is determined by the active permission
mode (see above).

### Rule schema

```yaml
# .bernstein/rules.yaml
permission_rules:
  - id: deny-force-push           # Unique identifier (required)
    action: deny                  # deny | ask | allow
    severity: critical            # critical | high | medium | low
    tool: Bash                    # Glob matched against tool name (case-insensitive)
    command: "git push *--force*" # Glob matched against command string
    description: "Block force pushes to any remote"

  - id: allow-read-src
    action: allow
    severity: low
    tool: Read
    path: "src/**"                # Glob matched against file_path / path arguments

  - id: ask-write-config
    action: ask
    severity: high
    tool: Write
    path: "*.yaml"
    description: "Require approval before writing YAML files"
```

**Field reference:**

| Field         | Required | Default    | Description                                              |
|---------------|----------|------------|----------------------------------------------------------|
| `id`          | yes      | —          | Unique rule identifier for logging and audit             |
| `action`      | yes      | —          | `deny`, `ask`, or `allow`                                |
| `severity`    | no       | `medium`   | Controls which permission modes enforce this rule        |
| `tool`        | no       | `*`        | Glob matched against tool name (case-insensitive)        |
| `path`        | no       | (any)      | Glob matched against `file_path`/`path` in tool input    |
| `command`     | no       | (any)      | Glob matched against `command` in tool input (Bash tool) |
| `description` | no       | `""`       | Human-readable purpose, shown in approval prompts        |

Path patterns support `**` for deep matching: `src/**` matches `src/foo/bar.py`.
All unspecified patterns act as wildcards (match anything).

### Example rule set

```yaml
# .bernstein/rules.yaml
permission_rules:
  # Always block destructive git operations
  - id: deny-force-push
    action: deny
    severity: critical
    tool: Bash
    command: "git push *--force*"
    description: "Block force pushes"

  - id: deny-reset-hard
    action: deny
    severity: critical
    tool: Bash
    command: "git reset --hard*"
    description: "Block hard resets"

  # Block agents writing to CI configuration
  - id: deny-write-ci
    action: deny
    severity: high
    tool: Write
    path: ".github/**"
    description: "Agents must not modify CI configuration"

  # Require approval for dependency changes
  - id: ask-write-lockfile
    action: ask
    severity: high
    tool: Write
    path: "*.lock"
    description: "Dependency lockfile changes require human review"

  # Allow agents to read freely within src/
  - id: allow-read-src
    action: allow
    severity: low
    tool: Read
    path: "src/**"
```

## Policy-as-code engine

The permission rule engine controls individual tool calls at runtime. The policy-as-code
engine is a separate layer that runs at merge time — it evaluates agent-produced diffs
against YAML or Rego policies before any changes are merged to your branch.

Policies live in `.sdd/policies/`. Bernstein loads all `*.yaml`, `*.yml`, and `*.rego`
files from that directory automatically. No restart is required; policies are re-read on
each merge gate evaluation.

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
    rule: "file_path !~ /\\.env$/"
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

A `block` severity violation prevents the merge. A `warn` severity violation logs a
warning and records it to `.sdd/metrics/` but does not block.

### Rego policies (OPA)

For more expressive policies, write Rego rules and place them in `.sdd/policies/`.
Bernstein invokes the `opa` binary if it is available on `$PATH`; if not, Rego policies
are skipped with a log warning.

```rego
# .sdd/policies/test-coverage.rego
package bernstein.merge

import future.keywords

# Block merges that add source files without corresponding tests.
deny[msg] {
    input.files_changed > 0
    not any_test_file_changed
    msg := "Source changed without test coverage"
}

any_test_file_changed if {
    some file in input.files
    regex.match(`tests/.*\.py$`, file.path)
}
```

The `input` object passed to Rego contains:
- `input.task.id`, `input.task.title`, `input.task.description`, `input.task.role`
- `input.diff_text` — full git diff
- `input.files` — array of `{ "path": "...", "content": "..." }`
- `input.files_changed` — count of changed files

Install OPA:

```bash
# macOS
brew install opa

# Linux
curl -L -o /usr/local/bin/opa https://openpolicyagent.org/downloads/latest/opa_linux_amd64_static
chmod 755 /usr/local/bin/opa
```

### View policy violations

Policy violations are written to `.sdd/metrics/` and surfaced in the task recap:

```bash
bernstein recap                     # Shows violations alongside task results
bernstein trace <task-id>           # Per-task policy evaluation detail
```

All violations — block and warn — are included in the compliance report:

```bash
bernstein admin compliance-report
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
| `devops`    | `.github/*`, `Dockerfile`, `docker-compose.yml`, `scripts/*`, `Makefile` | `.sdd/*`, `src/*`, `templates/roles/*` |
| `docs`      | `docs/*`, `README.md`, `CHANGELOG.md`, `CONTRIBUTING.md`   | `.github/*`, `.sdd/*`, `src/*`, `tests/*`, `templates/roles/*` |
| `manager`   | `docs/*`, `.sdd/backlog/*`, `plans/*`                       | `src/*`, `tests/*`, `.github/*`          |
| `architect` | `src/*`, `tests/*`, `docs/*`, `scripts/*`                   | `.github/*`, `.sdd/*`, `templates/roles/*` |

Path traversal is enforced at the filesystem level: paths containing `..`, null bytes,
URL-encoded traversal sequences (`%2e%2e`, `%2f`), or symlinks that escape the project root
are blocked unconditionally, regardless of role permissions.

### Override role permissions

Add a `roles:` section to `bernstein.yaml` to replace the defaults for specific roles:

```yaml
# bernstein.yaml
roles:
  backend:
    allowed_paths:
      - "src/**"
      - "tests/**"
    denied_paths:
      - ".env"
      - "secrets/**"
      - "*.pem"
      - "*.key"
  docs:
    allowed_paths:
      - "docs/**"
      - "README.md"
    denied_paths: []
```

## Sandbox setup

### File system restrictions

Restrict which directories all agents can access globally, in addition to role-specific limits:

```yaml
# bernstein.yaml
sandbox:
  allowed_paths:
    - "src/"
    - "tests/"
    - "docs/"
  denied_paths:
    - ".env"
    - ".env.*"
    - ".sdd/config/"
    - "credentials/"
    - "*.pem"
    - "*.key"
    - "secrets/**"
```

### Command restrictions

Control which shell commands agents can execute:

```yaml
# bernstein.yaml
command_policy:
  allowed:
    - "git *"
    - "npm *"
    - "python *"
    - "pytest *"
    - "uv *"
  denied:
    - "rm -rf *"
    - "curl * | sh"
    - "wget * | sh"
    - "sudo *"
    - "chmod 777 *"
    - "* > /dev/null 2>&1 &"
```

### Git worktree isolation

By default, each agent runs in an isolated git worktree under `.sdd/worktrees/`. This means:
- Agents cannot access each other's in-progress changes
- A failing agent cannot corrupt the main branch
- Merge to main only happens after the janitor verifies the output

To inspect an agent's worktree before merge:

```bash
bernstein diff <task-id>    # Show the agent's git diff
bernstein trace <task-id>   # Show decision trace
```

## Audit mode

### Enable the audit log

The audit log records all task lifecycle events with HMAC-chained entries for tamper evidence.
It is enabled by default:

```yaml
# bernstein.yaml
audit:
  enabled: true
  retention_days: 90
  archive_compressed: true
```

Each entry in `.sdd/metrics/audit.jsonl` contains a chain hash linking it to the previous
entry. Any insertion, deletion, or modification breaks the chain.

### Verify audit log integrity

```bash
# Verify the full HMAC chain is intact
bernstein admin verify-audit-log

# Check a specific date range
bernstein admin verify-audit-log --from 2026-01-01 --to 2026-03-31
```

### Export to SIEM

Forward audit logs to your security information and event management system:

```yaml
# bernstein.yaml
audit_export:
  target: "splunk"  # splunk | elasticsearch | cloudwatch
  splunk:
    endpoint: "https://splunk.yourcompany.com:8088"
    token: "${SPLUNK_HEC_TOKEN}"
    index: "bernstein-audit"
  elasticsearch:
    endpoint: "https://es.yourcompany.com:9200"
    index: "bernstein-audit"
    api_key: "${ES_API_KEY}"
```

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

Run a manual security review on any diff:

```bash
bernstein review --security <task-id>
```

## Secret management

### Use environment variables, never config files

```bash
export BERNSTEIN_JWT_SECRET="$(openssl rand -hex 32)"
export ANTHROPIC_API_KEY="sk-ant-..."
export OPENAI_API_KEY="sk-..."
export OIDC_CLIENT_SECRET="..."
```

Reference environment variables in `bernstein.yaml` with `${VAR_NAME}`:

```yaml
auth:
  jwt_secret: "${BERNSTEIN_JWT_SECRET}"   # Not the literal value
```

### Rotate secrets

Rotate the JWT signing secret and audit log HMAC key periodically:

```bash
# Generate a new JWT secret
export BERNSTEIN_JWT_SECRET="$(openssl rand -hex 32)"
bernstein stop && bernstein run   # Restart to pick up new secret

# Rotate the audit log HMAC key (audit-043: key lives OUTSIDE .sdd/ by default).
# Default location: $XDG_STATE_HOME/bernstein/audit.key
#   (falls back to ~/.local/state/bernstein/audit.key)
# Override with: export BERNSTEIN_AUDIT_KEY_PATH=/secure/path/audit.key
KEY_PATH="${BERNSTEIN_AUDIT_KEY_PATH:-${XDG_STATE_HOME:-$HOME/.local/state}/bernstein/audit.key}"
cp "$KEY_PATH" "${KEY_PATH}.bak"
bernstein admin rotate-audit-key
```

### PII detection

Enable the PII gate to scan agent outputs before they are written to disk:

```yaml
# bernstein.yaml
pii_gate:
  enabled: true
  patterns:
    - "\\b\\d{3}-\\d{2}-\\d{4}\\b"                              # SSN
    - "\\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\\.[A-Z]{2,}\\b"           # Email
    - "\\b4[0-9]{12}(?:[0-9]{3})?\\b"                           # Visa card number
  action: "redact"   # redact | block | warn
```

## Network isolation

### Agent outbound access

Prevent agents from making unauthorized network calls:

```yaml
# bernstein.yaml
agent_network:
  allow_outbound: false          # Block all outbound traffic by default
  allowed_hosts:
    - "api.anthropic.com"        # Required for Claude agents
    - "api.openai.com"           # Required for Codex agents
    - "generativelanguage.googleapis.com"  # Required for Gemini agents
```

### Isolation levels

The network isolation validator checks agent connectivity against a policy:

| Level        | Description                                      |
|--------------|--------------------------------------------------|
| `none`       | No network access permitted                      |
| `local_only` | Only localhost/loopback (127.0.0.1, ::1)         |
| `restricted` | Only explicitly listed endpoints are reachable   |
| `full`       | Unrestricted (development only)                  |

```yaml
# bernstein.yaml
agent_network:
  isolation_level: restricted    # none | local_only | restricted | full
  allowed_endpoints:
    - host: "api.anthropic.com"
      port: 443
    - host: "api.openai.com"
      port: 443
```

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

## API authentication

### JWT authentication (recommended for single-tenant)

```yaml
# bernstein.yaml
auth:
  enabled: true
  method: "jwt"
  jwt_secret: "${BERNSTEIN_JWT_SECRET}"
  token_expiry_s: 3600
```

Generate a token for API access:

```bash
bernstein auth token --expiry 24h
```

Include the token in API requests:

```bash
curl -H "Authorization: Bearer <token>" http://127.0.0.1:8052/status
```

### OIDC / SSO (recommended for multi-user or enterprise)

```yaml
# bernstein.yaml
auth:
  enabled: true
  method: "oidc"
  oidc:
    issuer_url: "https://idp.yourcompany.com"
    client_id: "bernstein-prod"
    client_secret: "${OIDC_CLIENT_SECRET}"
    redirect_uri: "https://bernstein.yourcompany.com/auth/callback"
    scopes: ["openid", "profile", "email", "groups"]
    role_mapping:
      platform-admins: "admin"
      dev-team: "operator"
      default: "viewer"
```

### Cluster node authentication

In multi-node deployments, worker nodes authenticate to the task server using
scoped JWT tokens. Tokens have three scopes:

| Scope             | Used for                              |
|-------------------|---------------------------------------|
| `node:register`   | Node registration on startup          |
| `node:heartbeat`  | Periodic heartbeat pings              |
| `node:admin`      | Administrative operations             |

```yaml
# bernstein.yaml
cluster:
  auth:
    enabled: true
    secret: "${BERNSTEIN_CLUSTER_SECRET}"
    token_expiry_hours: 24
```

Revoke a node token to immediately deny a compromised worker:

```bash
bernstein admin revoke-node <node-id>
```

### Dashboard authentication

```yaml
# bernstein.yaml
dashboard_auth:
  enabled: true
  password: "${BERNSTEIN_DASHBOARD_PASSWORD}"
  session_timeout_s: 1800
```

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

```yaml
# bernstein.yaml
rate_limits:
  "/tasks":
    requests_per_minute: 100
    burst: 20
  "/tasks/*/complete":
    requests_per_minute: 50
    burst: 10
```

## Compliance reporting

### Built-in compliance checks

Run a full security posture check against your current configuration:

```bash
bernstein doctor --security
```

Output includes:
- Authentication status (enabled/disabled)
- TLS configuration check
- Audit log integrity status
- Agent sandbox configuration
- Permission mode in effect
- PII detection status
- Dependency vulnerability summary

### Generate a compliance report

```bash
# Structured JSON output for ingestion into compliance tooling
bernstein admin compliance-report --format json > compliance-$(date +%Y%m%d).json

# Human-readable summary
bernstein admin compliance-report
```

The report covers:
- **Authentication**: auth method, token expiry, MFA presence
- **Audit**: log enabled, chain integrity, retention policy, SIEM export
- **Network**: isolation level, IP allowlist, TLS status
- **Data**: PII gate, encryption at rest, data residency policy
- **Dependencies**: vulnerability scan results, license compliance
- **Access control**: permission mode, rule count, role coverage

### Dependency scanning

Scan agent-introduced dependencies for known vulnerabilities and license issues:

```yaml
# bernstein.yaml
dependency_scan:
  enabled: true
  block_on_critical: true
  allowed_licenses:
    - "MIT"
    - "Apache-2.0"
    - "BSD-2-Clause"
    - "BSD-3-Clause"
    - "ISC"
```

### Data residency

Restrict agent operations to specific regions in multi-region deployments:

```yaml
# bernstein.yaml
data_residency:
  tenant_policies:
    eu-tenant:
      allowed_regions: ["eu-west", "eu-central"]
      enforce_strict: true
```

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
- [ ] `bernstein doctor --security` passes clean
- [ ] `bernstein admin compliance-report` reviewed and archived
