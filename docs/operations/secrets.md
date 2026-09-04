# Secrets and credentials

Bernstein has two distinct secret stores and a small set of provider env
vars that connect them. This page is the one place to look when you are
asking *where does this token live, who can read it, and how does it
reach the agent*.

If you are instead asking *which env vars get filtered out when an
agent is spawned*, see [`env-isolation.md`](env-isolation.md). The two
docs are siblings: env-isolation is about *what is dropped on the way
to the subprocess*; this page is about *what is stored on disk and how
it is loaded in the first place*.

---

## Overview

Bernstein recognises four distinct ways a secret can reach the
orchestrator:

1. **The credential vault** - third-party developer credentials
   (GitHub, Linear, Jira, Slack, Telegram) the user `bernstein
   connect`-ed once. Stored in the OS keychain by default; AES-GCM
   file blob on headless boxes. This is the canonical place for
   *human-supplied* tokens.
2. **External secret managers** - HashiCorp Vault, AWS Secrets
   Manager, or 1Password CLI. Used when the operator already runs a
   secrets manager and wants Bernstein to *read* from it at startup
   (`secrets.py`) or *inject* short-lived credentials at agent spawn
   time (`vault_injector.py`).
3. **Provider env vars** - `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`,
   `CLOUDFLARE_API_TOKEN`, `GOOGLE_API_KEY`, etc. Read directly from
   the orchestrator's environment by individual adapters and bridges.
   This is the path most operators start with, and is still the
   supported way to feed LLM-provider keys.
4. **`.env` files** - Bernstein does **not** auto-load `.env` files.
   If you keep your provider keys in a `.env`, your shell or process
   manager (`direnv`, `systemd`, `docker compose env_file`, etc.) is
   responsible for sourcing them before launching `bernstein`. See
   [.env file conventions](#env-file-conventions) below.

The rest of this page covers each store in detail and ends with
operator best practices.

---

## Provider env vars

This is the table of provider env vars Bernstein reads. Each row is
either an LLM/model provider (consumed by adapters and the routing
layer) or an integration provider (consumed by a feature like the
GitHub App, Datadog APM, or the Cloudflare bridges).

| Provider               | Variables                                                                          | Read in                                                            |
|------------------------|------------------------------------------------------------------------------------|--------------------------------------------------------------------|
| Anthropic / Claude     | `ANTHROPIC_API_KEY`                                                                | `adapters/claude.py`, `adapters/aider.py`, `adapters/amp.py`        |
| OpenAI                 | `OPENAI_API_KEY`, `OPENAI_ORG_ID`, `OPENAI_ORGANIZATION`, `OPENAI_BASE_URL`        | `adapters/codex.py`, `adapters/qwen.py`, `adapters/aider.py`        |
| Azure OpenAI           | `AZURE_OPENAI_API_KEY`                                                             | `adapters/aider.py`                                                 |
| Google AI / Gemini     | `GOOGLE_API_KEY` or `GEMINI_API_KEY`, `GOOGLE_CLOUD_PROJECT`, `GOOGLE_APPLICATION_CREDENTIALS` | `adapters/gemini.py`                                       |
| Cloudflare             | `CLOUDFLARE_API_TOKEN` (alias `CF_API_TOKEN`), `CLOUDFLARE_ACCOUNT_ID` (alias `CF_ACCOUNT_ID`) | `bridges/cloudflare*.py`, `bridges/browser_rendering.py`, `bridges/r2_sync.py` |
| Cody / Sourcegraph     | `SRC_ACCESS_TOKEN`, `SRC_ENDPOINT`                                                 | `adapters/amp.py`                                                   |
| Kiro                   | `KIRO_API_KEY`                                                                     | `adapters/kiro.py`                                                  |
| Kilo                   | `KILO_API_KEY`                                                                     | `adapters/kilo.py`                                                  |
| OpenRouter             | `OPENROUTER_API_KEY_FREE`, `OPENROUTER_API_KEY_PAID`                               | `core/routing/openrouter.py`                                        |
| GitHub (legacy env)    | `GITHUB_TOKEN`                                                                     | `core/security/vault/resolver.py` (legacy fallback for vault)       |
| Linear (legacy env)    | `LINEAR_API_KEY`                                                                   | `core/security/vault/resolver.py` (legacy fallback for vault)       |
| Jira (legacy env)      | `JIRA_BASE_URL`, `JIRA_EMAIL`, `JIRA_API_TOKEN`, `JIRA_USER_EMAIL`, `JIRA_ISSUE_KEY` | `core/security/vault/resolver.py`, `cli/commands/ticket_cmd.py`   |
| Slack (legacy env)     | `BERNSTEIN_SLACK_TOKEN`, `SLACK_BOT_TOKEN`, `SLACK_WEBHOOK_URL`                    | `core/security/vault/resolver.py`, `core/notifications/slack.py`    |
| Telegram (legacy env)  | `BERNSTEIN_TELEGRAM_TOKEN`, `TELEGRAM_BOT_TOKEN`                                   | `core/security/vault/resolver.py`, `core/notifications/telegram.py` |
| GitHub App             | `GITHUB_APP_ID`, `GITHUB_APP_PRIVATE_KEY`, `GITHUB_WEBHOOK_SECRET`                 | `core/git/github_app.py`                                            |
| Datadog                | `DD_API_KEY` (alias `DATADOG_API_KEY`), `DD_SITE`, `DD_SERVICE`                    | `core/observability/apm_integration.py`                             |
| PagerDuty              | `PAGERDUTY_ROUTING_KEY`                                                            | `core/notifications/pagerduty.py`                                   |
| Bernstein server auth  | `BERNSTEIN_AUTH_TOKEN`, `BERNSTEIN_AUTH_JWT_SECRET`                                | `core/security/auth_middleware.py`                                  |
| Vault file backend     | `BERNSTEIN_VAULT_BACKEND`, `BERNSTEIN_VAULT_PASSPHRASE_ENV` (and the env-var it names) | `core/security/vault/factory.py`                                |
| HashiCorp Vault server | `VAULT_ADDR`, `VAULT_TOKEN`                                                        | `core/security/secrets.py`, `core/security/vault_injector.py`        |

The rule of thumb is: anything `*_API_KEY` / `*_API_TOKEN` /
`*_BOT_TOKEN` / `*_WEBHOOK_*` is a secret; anything `*_BASE_URL` /
`*_ACCOUNT_ID` / `*_PROJECT` is configuration that is fine to log.

`bernstein doctor` prints a diff of which provider env vars are set
versus which ones any active feature actually needs.

---

## The `bernstein creds` group

The credential vault is the canonical place for the five third-party
providers Bernstein integrates with directly: GitHub, Linear, Jira,
Slack, and Telegram. (LLM-provider keys still come from env vars; the
vault is for *developer-tool* credentials, not model API keys.)

The vault has two top-level CLI commands:

- `bernstein connect <provider>` - guided paste or OAuth-device-code
  flow that validates the credential against the provider's whoami
  endpoint and stores it. (Lives at
  `cli/commands/creds_cmd.py:95`.)
- `bernstein creds <list|revoke|test>` - inspect and manage what is
  already stored. (Lives at `cli/commands/creds_cmd.py:214`.)

### `bernstein connect`

```bash
bernstein connect github           # paste a personal access token
bernstein connect linear --oauth   # OAuth device-code flow
bernstein connect jira             # email + base URL + API token
bernstein connect slack            # paste a bot token (xoxb-...)
bernstein connect telegram         # paste a bot token (123:ABC-DEF...)
```

Behaviour:

1. The command consults `core/security/vault/providers.py` for the
   prompt list. Secret fields are read with `getpass.getpass()` so the
   token never echoes; non-secret fields (Jira email, base URL) use
   `click.prompt`.
2. The supplied secret is sent to the provider's whoami endpoint. On
   success, the user's account label (e.g. GitHub login,
   Atlassian email) is recorded. On failure the secret is **not**
   stored - you see the masked token and the error.
3. The validated secret + metadata is written to the vault, and an
   audit event of type `vault.connect` is appended to
   `.sdd/audit/YYYY-MM-DD.jsonl`.

### `bernstein creds list`

Prints a table with provider, account, fingerprint, created-at,
last-used-at. **Never** prints the secret itself. The fingerprint is a
12-character SHA-256 prefix used to identify a token without exposing
it.

```text
Provider  Account                Fingerprint   Created              Last used
github    alex-octocat           ab12cd34ef56  2026-04-25T12:00:00  2026-05-04T08:14:21
slack     bernstein-bot          0099aabbccdd  2026-04-26T10:11:02  never
```

### `bernstein creds revoke <provider>`

Removes the entry from the local vault. For providers that expose a
self-service revoke endpoint (currently Slack via
`auth.revoke`), the CLI also calls that endpoint so the upstream token
is invalidated. For providers without a programmatic revoke (GitHub
PATs, Jira API tokens, Telegram bot tokens), the CLI deletes the local
copy and prints a hint pointing to the provider's UI for full
rotation.

### `bernstein creds test <provider>`

Re-validates a stored credential against the provider's whoami
endpoint. Use this after a long pause to confirm a token has not been
revoked upstream, or as a smoke test in CI.

### Backend selection (`--backend`)

Both `connect` and the `creds` subcommands accept:

- `--backend keyring` (default) - store in the OS keychain.
- `--backend file --passphrase-env VAR` - store in
  `~/.config/bernstein/vault.enc`, encrypted with a passphrase read
  from `$VAR`.

The default can also be set via:

- `BERNSTEIN_VAULT_BACKEND=keyring|file`
- `BERNSTEIN_VAULT_PASSPHRASE_ENV=NAME_OF_ENV_VAR_THAT_HOLDS_THE_PASSPHRASE`

so containers can opt into the file backend without plumbing CLI flags
through every entry point.

---

## CredentialVault internals

Mechanism by backend, source of truth in
`src/bernstein/core/security/vault/`:

### Keyring backend (`backend_keyring.py`)

Default. Delegates to the OS keychain via the `keyring` package:

| OS      | Backend                                                       |
|---------|---------------------------------------------------------------|
| macOS   | Keychain Services                                             |
| Linux   | Secret Service / libsecret (gnome-keyring, KDE Wallet, ...)   |
| Windows | Credential Manager via DPAPI                                  |

Each provider gets a separate account string under the service name
`bernstein`. The stored value is a JSON envelope:

```json
{
  "secret": "...",
  "account": "alex@example.com",
  "fingerprint": "ab12cd34ef56",
  "created_at": "2026-04-25T12:00:00Z",
  "last_used_at": null,
  "metadata": {"scope": "repo,issues"}
}
```

The keychain handles encryption-at-rest; the backend is a thin
serialiser. Who can decrypt is whoever holds the OS user session -
unlock the keychain, you read the secret. There is no Bernstein-level
master key.

A small "provider index" entry (`__bernstein_provider_index__`) is
maintained alongside the per-provider entries because OS keychain APIs
do not expose a portable "list all entries for service X" operation.

### File backend (`backend_file.py`)

Opt-in fallback for headless boxes (containers, CI, hardened servers
without a desktop session). Encrypts a single JSON blob to
`~/.config/bernstein/vault.enc` (mode `0600`).

Crypto details:

- AES-256-GCM via `cryptography.hazmat.primitives.ciphers.aead.AESGCM`.
- 32-byte key derived via PBKDF2-HMAC-SHA256, **200 000 iterations**,
  16-byte random salt stored in the file header.
- Fresh 12-byte nonce on every write; concatenated nonce | ciphertext
  | GCM tag, base64-encoded.
- Atomic temp-file + rename so a crash leaves the previous vault
  intact instead of producing a half-written file.

The passphrase is **always** read from an env var named by
`--passphrase-env` (or `$BERNSTEIN_VAULT_PASSPHRASE_ENV`). The backend
refuses to start if that env var is unset or empty - booting with no
protection would silently downgrade security versus the keyring
backend.

Who can decrypt: anyone who can `cat vault.enc` *and* read the
passphrase env var. In practice that means whoever runs the Bernstein
process. Treat the passphrase env var the same way you would treat the
secrets the vault protects.

### Audit chain (`audit.py`)

Every connect, read, revoke, and test writes a `vault.{action}` entry
into `.sdd/audit/YYYY-MM-DD.jsonl` via
`bernstein.core.security.audit.AuditLog`. Entries record provider id,
account label, fingerprint, and backend - **never** the secret
material itself. The audit log is HMAC-chained so tampering is
detectable.

Audit failures are deliberately non-fatal on read paths: a broken
audit setup logs a warning rather than locking the user out of their
own credentials. Connect and revoke do raise on audit failure so
misconfiguration is visible.

### Vault-first resolution (`resolver.py`)

Higher-level commands like `bernstein from-ticket`, `bernstein chat`,
and `bernstein pr` call `resolve_secret(provider_id, vault=...)` rather
than reading env vars directly. The resolver tries the vault first;
on miss it falls back to the legacy env var (e.g. `GITHUB_TOKEN` for
GitHub) and emits a one-time `DeprecationWarning` per (provider,
env-var) pair. This is the migration path: existing users keep
working, but they are nudged toward `bernstein connect`.

---

## External secret managers (`secrets.py` / `vault_injector.py`)

For operators who already run a secrets manager, Bernstein has two
integration points. They are completely independent of the
`bernstein creds` vault above.

### Startup-time loading: `core/security/secrets.py`

Configured via `bernstein.yaml`:

```yaml
secrets:
  provider: vault          # or "aws", "1password"
  path: secret/bernstein   # provider-specific path/ARN
  ttl: 300                 # cache for 5 minutes
  field_map:               # rename secret fields → env-var names
    anthropic_key: ANTHROPIC_API_KEY
    openai_key:    OPENAI_API_KEY
```

On startup, `load_secrets()` fetches the named secret, applies
`field_map`, caches the result with the configured TTL, and exposes
the values as environment variables for the rest of the orchestrator
to consume. A background `SecretsRefresher` thread refreshes at 80% of
the TTL so spawned agents do not stall on a sync re-fetch.

Supported providers:

- **HashiCorp Vault** - KV v2 API, `VAULT_ADDR` + `VAULT_TOKEN`.
- **AWS Secrets Manager** - boto3, picks up the standard AWS auth
  chain (env / profile / IAM role).
- **1Password** - shells out to the `op` CLI (`op item get`); requires
  the user to have `op signin`-ed.

If the provider is unreachable, Bernstein falls back to env-var values
for any names appearing in `field_map.values()` so a transient outage
does not crash the orchestrator.

### Spawn-time injection: `core/security/vault_injector.py`

Same three providers, different lifecycle. The injector is for
*ephemeral, per-agent* credentials:

- **Vault dynamic secrets**: creates a short-lived lease, revokes via
  `/v1/sys/leases/revoke` when the agent exits.
- **AWS STS**: requests `assume_role` or `get_session_token` with a
  short duration; credentials expire automatically.
- **1Password**: reads a static item; the value is cleared from the
  returned dict after injection so it cannot be re-read later in the
  same process.

Use the injector when you want an agent to have a database password
or a cloud role for the lifetime of one task and never see it again.

### Broker-side stores: `core/security/external_secret_store.py`

Both integration points above copy a value out of the operator's store
and into Bernstein. The broker
(`core/security/secrets_broker.py`) takes the opposite approach: the
operator's store stays the only place the value lives, and Bernstein
keeps the authorization record.

Configure it as a broker backend:

```yaml
secrets_broker:
  backend: external
  backend_settings:
    store_name: acme       # a registered store
    # any further keys are forwarded to that store's factory
```

A spec then names a secret by opaque reference, never by value:

```yaml
secret_name: acme:prod/db-password
```

The `acme` half selects the store; the rest is store-native (a Vault
path, an ARN, a resource id) and Bernstein never interprets it.

On every mint the broker asks the store three things, in order:

1. `resolve(path)` - non-secret facts: which store holds it, its
   store-native version or lease id, whether it is revoked, and any
   store-imposed expiry.
2. `report_revocation(path, upstream_id=...)` - revoking in your own
   store is what stops Bernstein minting. There is no separate
   revocation list to keep in step.
3. `mint_credential(path, audience=..., ttl_seconds=...)` - a
   short-lived credential. A store may return a shorter expiry than
   asked for and the broker caps the token to it, so a broker token
   never outlives the credential behind it.

What reaches the grant chain is the grant, the store identity, the
audience and the expiry. The credential value is held in the broker's
in-process registry for the token's lifetime and is dropped on
revocation. Grant enforcement is unchanged: without a verifying grant
the broker refuses, and the store is never called.

Binding a credential for one step:

```python
with broker.bind_scoped(
    secret_name="acme:prod/db-password",
    task_id=task.id,
    env_var="DATABASE_PASSWORD",
    grant=grant,
) as token:
    run_step(token)
# DATABASE_PASSWORD is gone here - or back to whatever you had set
# under that name - and the token is revoked.
```

#### Registering a store

No concrete store ships built in. A store implements
`ExternalSecretStore` and registers under a name, either through the
`provide_secret_store` plugin hook or, in-process, through
`register_secret_store()`. Because stores register from outside,
`bernstein.core` never imports a vendor SDK -
`tests/unit/security/test_core_vendor_sdk_imports.py` fails the build
if it gains one.

---

## `.env` file conventions

Bernstein does not call `dotenv.load_dotenv()` itself - there is no
implicit `.env` discovery, no `python-dotenv` dependency in the
runtime, and no `--env-file` flag on `bernstein` or `bernstein run`.

If you want to use a `.env` file, your launch wrapper has to load it.
The conventional patterns:

| Wrapper            | How to source `.env` before `bernstein` runs        |
|--------------------|-----------------------------------------------------|
| Shell + direnv     | Drop `dotenv` into `.envrc`; `direnv allow`         |
| systemd unit       | `EnvironmentFile=/etc/bernstein/.env` in the unit   |
| docker compose     | `env_file: .env` on the bernstein service           |
| Kubernetes         | `envFrom: secretRef:` from the chart values         |
| GitHub Actions     | `env:` block at job/step level, fed from secrets    |

Precedence inside Bernstein, when multiple paths populate the same
key:

1. Variables explicitly set in the orchestrator's environment win.
2. Values written by the secrets-manager loader (`secrets.py`) at
   startup overlay onto `os.environ`.
3. Per-agent injections (`vault_injector.py`) overlay on top of the
   inherited env when the agent subprocess is spawned, but only for
   the keys named in `env_map`.
4. The credential vault is consulted last via `resolver.py`, and only
   for the five providers it manages - the vault never overrides an
   already-set legacy env var, only fills in when the env var is
   missing or empty.

Two practical consequences:

- A `.env` change does not take effect for a running orchestrator;
  restart Bernstein.
- If you have both `GITHUB_TOKEN` in the environment **and**
  `bernstein connect github` ran, the env var wins and you get the
  deprecation warning. Run `bernstein creds revoke github` to drop
  the vault entry, or unset the env var to let the vault take over.

---

## Sibling concern: env-var isolation

The flow above gets secrets *into* the orchestrator. A separate,
equally important flow gates which secrets reach a spawned agent
subprocess:

- This page (`secrets.md`): "where do secrets live and how are they
  loaded into the orchestrator process?"
- [`env-isolation.md`](env-isolation.md): "when the orchestrator
  spawns an agent, which of those env vars actually get passed
  through?"

Even if the orchestrator has `STRIPE_SECRET_KEY` and `DATABASE_URL`
loaded, a spawned agent does **not** see them - `build_filtered_env()`
returns a fresh dict containing only the base allowlist plus a small
per-adapter set of provider keys. See `env-isolation.md` for the
allowlist, the per-adapter extras, and the verification recipe.

This split is intentional: it lets the orchestrator be the trusted
component that holds secrets, and lets each agent be a much smaller
blast radius.

---

## Best practices

1. **Never commit secrets.** A `.gitignore` entry for `.env` and
   `vault.enc` plus a pre-commit hook (`gitleaks`, `truffleHog`, or
   the built-in DLP scanner at
   `core/security/dlp_scanner_v2.py`) catches the common cases.

2. **Prefer the vault for developer credentials.** GitHub PATs, Jira
   tokens, and Slack bot tokens belong in
   `bernstein connect <provider>`. The env-var path is a migration
   compatibility shim, not the recommended steady state.

3. **Prefer external secrets managers for service credentials.** If
   you already run Vault / AWS Secrets Manager / 1Password, point
   `secrets.provider` at it rather than baking keys into a `.env`.
   Use `vault_injector.py` for anything that should live for one
   agent run only (database passwords, cloud roles).

4. **Rotate on a schedule.** `bernstein creds test <provider>` plus a
   cron entry catches expired tokens before the next ticket import
   fails. For provider keys without a rotate endpoint (Telegram,
   Jira), document the manual rotation in a runbook.

5. **Use `--passphrase-env` carefully.** The file-backend passphrase
   is the master key for every credential the vault holds. Treat it
   the same way you would treat the secrets it protects: do not log
   it, do not check it into Ansible plaintext, do not put it in a
   shared `.env`.

6. **Multi-environment setups.** Run separate vaults per environment
   (dev / staging / prod) by setting `BERNSTEIN_VAULT_PASSPHRASE_ENV`
   to a different env-var name per environment, and pointing
   `~/.config/bernstein/vault.enc` at a per-environment path with
   `--file-path` (or a different home directory). Do **not** share a
   single vault across environments - a leaked dev passphrase should
   never give access to production credentials.

7. **Mask, don't print.** Any custom code that touches secrets should
   import `mask_secret` and `fingerprint` from
   `core.security.vault.resolver` rather than rolling its own
   redaction. The vault's CLI output and audit log already use them.

---

## Code pointers

| Concern                       | File                                                               |
|-------------------------------|--------------------------------------------------------------------|
| `bernstein connect/creds` CLI | `src/bernstein/cli/commands/creds_cmd.py`                          |
| Vault protocol + dataclasses  | `src/bernstein/core/security/vault/protocol.py`                    |
| Backend selector              | `src/bernstein/core/security/vault/factory.py`                     |
| OS keychain backend           | `src/bernstein/core/security/vault/backend_keyring.py`             |
| AES-GCM file backend          | `src/bernstein/core/security/vault/backend_file.py`                |
| Provider registry + whoami    | `src/bernstein/core/security/vault/providers.py`                   |
| Connect / test / revoke flow  | `src/bernstein/core/security/vault/connect.py`                     |
| Vault-first resolver          | `src/bernstein/core/security/vault/resolver.py`                    |
| HMAC audit log                | `src/bernstein/core/security/vault/audit.py`                       |
| Startup secret-manager loader | `src/bernstein/core/security/secrets.py`                           |
| Spawn-time injector           | `src/bernstein/core/security/vault_injector.py`                    |
| Secrets broker                | `src/bernstein/core/security/secrets_broker.py`                    |
| External-store contract       | `src/bernstein/core/security/external_secret_store.py`             |
| External-store registry       | `src/bernstein/core/security/secret_store_registry.py`             |
| Env-var filter for spawns     | `src/bernstein/adapters/env_isolation.py` (see `env-isolation.md`) |

---

## Related

- [Environment variable isolation](env-isolation.md) - what gets
  filtered when Bernstein spawns an agent.
- [Security & identity](security-and-identity.md) - JWT/OIDC/SAML,
  RBAC, audit log integrity at the API layer.
- [Configuration](CONFIG.md) - full list of `bernstein.yaml` keys
  including `secrets.*`.
- [Cloudflare setup](../cloudflare/cloudflare-setup.md) - where the
  Cloudflare provider env vars are consumed.
