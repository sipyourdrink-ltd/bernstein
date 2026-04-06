# Security Hardening Guide

How to lock down a Bernstein deployment for production use.

## Authentication

### Enable API authentication

By default, the task server has no authentication. Enable it for any non-local deployment:

```yaml
# bernstein.yaml
auth:
  enabled: true
  method: "jwt"  # jwt | oidc | bearer
  jwt_secret: "${BERNSTEIN_JWT_SECRET}"  # Use env var, never hardcode
  token_expiry_s: 3600
```

### SSO/OIDC integration

For enterprise deployments, use OIDC with your identity provider:

```yaml
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

### Dashboard authentication

```yaml
dashboard_auth:
  enabled: true
  password: "${BERNSTEIN_DASHBOARD_PASSWORD}"
  session_timeout_s: 1800
```

## Network security

### IP allowlisting

Restrict API access to known networks:

```yaml
network:
  allowed_ips:
    - "10.0.0.0/8"         # Internal network
    - "172.16.0.0/12"      # VPN
    - "203.0.113.0/24"     # Office
```

Localhost (127.0.0.1, ::1) is always allowed. Health endpoints are exempt from IP checks.

### TLS termination

Bernstein does not terminate TLS directly. Use a reverse proxy:

```nginx
# nginx.conf
server {
    listen 443 ssl;
    server_name bernstein.yourcompany.com;

    ssl_certificate /etc/ssl/certs/bernstein.crt;
    ssl_certificate_key /etc/ssl/private/bernstein.key;
    ssl_protocols TLSv1.2 TLSv1.3;
    ssl_ciphers HIGH:!aNULL:!MD5;

    location / {
        proxy_pass http://127.0.0.1:8052;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

### Rate limiting

Per-endpoint rate limiting prevents abuse:

```yaml
rate_limits:
  "/tasks":
    requests_per_minute: 100
    burst: 20
  "/tasks/*/complete":
    requests_per_minute: 50
    burst: 10
```

For multi-tenant deployments, use per-tenant rate limiting (see ENT-008).

## Secrets management

### Environment variables

Never store secrets in configuration files. Use environment variables:

```bash
export BERNSTEIN_JWT_SECRET="$(openssl rand -hex 32)"
export OIDC_CLIENT_SECRET="your-oidc-secret"
export ANTHROPIC_API_KEY="sk-ant-..."
```

### Secret rotation

Rotate the audit log HMAC key periodically:

```bash
# Back up the old key
cp .sdd/config/audit-key .sdd/config/audit-key.bak

# Generate new key (Bernstein handles the transition)
bernstein admin rotate-audit-key
```

## Audit logging

### Enable immutable audit log

The audit log is enabled by default and uses HMAC-chained entries for tamper evidence:

```yaml
audit:
  enabled: true
  retention_days: 90
  archive_compressed: true
```

### Export to SIEM

Forward audit logs to your SIEM for centralized monitoring:

```yaml
audit_export:
  target: "splunk"  # splunk | elasticsearch | cloudwatch
  splunk:
    endpoint: "https://splunk.yourcompany.com:8088"
    token: "${SPLUNK_HEC_TOKEN}"
    index: "bernstein-audit"
```

### Audit log integrity verification

```bash
# Verify the HMAC chain is intact
bernstein admin verify-audit-log

# Check a specific date range
bernstein admin verify-audit-log --from 2026-01-01 --to 2026-03-31
```

## Agent sandboxing

### File system restrictions

Limit which directories agents can read and write:

```yaml
sandbox:
  allowed_paths:
    - "src/"
    - "tests/"
    - "docs/"
  denied_paths:
    - ".env"
    - ".sdd/config/"
    - "credentials/"
    - "*.pem"
    - "*.key"
```

### Command restrictions

Control which shell commands agents can execute:

```yaml
command_policy:
  allowed:
    - "git *"
    - "npm *"
    - "python *"
    - "pytest *"
  denied:
    - "rm -rf /"
    - "curl * | sh"
    - "sudo *"
```

### Network restrictions

Prevent agents from making unauthorized network calls:

```yaml
agent_network:
  allow_outbound: false  # Agents cannot make HTTP requests
  allowed_hosts:
    - "api.anthropic.com"
    - "api.openai.com"
```

## Data protection

### Data residency

Ensure data stays in configured regions:

```yaml
data_residency:
  tenant_policies:
    eu-tenant:
      allowed_regions: ["eu-west", "eu-central"]
      enforce_strict: true
```

### Encryption at rest

Encrypt the `.sdd` state directory:

```bash
# Linux: use LUKS for the volume
cryptsetup luksFormat /dev/sdb1
cryptsetup open /dev/sdb1 bernstein-state
mkfs.ext4 /dev/mapper/bernstein-state
mount /dev/mapper/bernstein-state /var/lib/bernstein

# macOS: use encrypted APFS volume
diskutil apfs addVolume disk1 APFS bernstein-state -passphrase
```

### PII detection

Enable the PII output gate to scan agent outputs:

```yaml
pii_gate:
  enabled: true
  patterns:
    - "\\b\\d{3}-\\d{2}-\\d{4}\\b"    # SSN
    - "\\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\\.[A-Z]{2,}\\b"  # Email
  action: "redact"  # redact | block | warn
```

## Dependency scanning

Scan agent-introduced dependencies for known vulnerabilities:

```yaml
dependency_scan:
  enabled: true
  block_on_critical: true
  allowed_licenses:
    - "MIT"
    - "Apache-2.0"
    - "BSD-2-Clause"
    - "BSD-3-Clause"
```

## Checklist

Before going to production:

- [ ] Authentication enabled (JWT, OIDC, or bearer)
- [ ] TLS termination via reverse proxy
- [ ] IP allowlisting configured
- [ ] Rate limiting enabled
- [ ] Audit logging active and exported to SIEM
- [ ] Agent sandboxing configured (file paths, commands)
- [ ] Secrets in environment variables, not config files
- [ ] `.sdd` directory on encrypted volume
- [ ] Backup and restore tested
- [ ] PII detection enabled
- [ ] Dependency scanning enabled
- [ ] Dashboard password set
- [ ] Health checks configured in load balancer
