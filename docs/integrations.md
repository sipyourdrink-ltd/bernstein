# Integrations

The systems Bernstein bridges to, under the names those systems use for
themselves.

This page exists because a capability that cannot be found by the words people
search for reads as absent. If you run Okta, Vault or OpenTelemetry, this is
the page that tells you whether we meet you there — and, where we do not,
whether that is a gap or a decision.

## How to read the table

Every cell has to be checkable, and
[`tests/unit/docs/test_integrations_index.py`](https://github.com/sipyourdrink-ltd/bernstein/blob/main/tests/unit/docs/test_integrations_index.py)
parses this page and asserts it:

- **Shipped** names a module that exists at `HEAD`. If the path stops
  resolving, the test fails.
- **Open** names an issue number. If the issue is closed, the row is stale and
  gets rewritten — not softened.
- **Not planned** carries one line of why. This is the column most such pages
  omit, and the one that saves an evaluator the most time.

A row that can cite none of the three is deleted rather than reworded. That is
the same constraint the compliance packs run under: a claim with no evidence
behind it is not a weaker claim, it is not a claim.

There is no "partially shipped" column. A row that has both a module and an
open issue expresses exactly that, and a fourth column would only hide which
half is which.

## Directories and identity providers

| Target | Shipped | Open | Not planned |
| --- | --- | --- | --- |
| SCIM 2.0 (RFC 7643/7644) | `src/bernstein/adapters/directory/scim.py` — provisioning resources mapped onto agent principals | [#5040](https://github.com/sipyourdrink-ltd/bernstein/issues/5040) — serve SCIM over HTTP so a directory can drive it | |
| Generic OIDC | `src/bernstein/core/security/auth.py` — `OIDCConfig`, dashboard and API login | | |
| SAML | `src/bernstein/core/security/auth.py` — alongside the OIDC flow | | |
| Okta | | [#5018](https://github.com/sipyourdrink-ltd/bernstein/issues/5018) — bridge agent principals to Okta groups | |
| Microsoft Entra ID | | [#5018](https://github.com/sipyourdrink-ltd/bernstein/issues/5018) — same bridge, same issue | |
| LDAP | | | No open issue and no module. SCIM is the provisioning path we invested in; an LDAP bind would be a second one. Worth an issue if you need it — this is a gap, not a decision. |

## Authorization and policy engines

| Target | Shipped | Open | Not planned |
| --- | --- | --- | --- |
| Open Policy Agent (Rego) | `src/bernstein/core/security/external_policy_hook.py` — `OPAHook`, REST API or CLI | [#4912](https://github.com/sipyourdrink-ltd/bernstein/issues/4912) — the hooks exist but no decision path consults them | |
| Cedar | `src/bernstein/core/security/external_policy_hook.py` — `CedarHook` | [#4912](https://github.com/sipyourdrink-ltd/bernstein/issues/4912) — same gap | |
| AuthZEN 1.0 | `src/bernstein/core/security/authzen.py` — the shape both hooks normalise through | [#5032](https://github.com/sipyourdrink-ltd/bernstein/issues/5032) — speak it at the decision boundary | |

## Secret stores

| Target | Shipped | Open | Not planned |
| --- | --- | --- | --- |
| HashiCorp Vault | `src/bernstein/core/security/secrets_broker.py` — `VaultBackend`, KV v2 | [#5021](https://github.com/sipyourdrink-ltd/bernstein/issues/5021) — leases and renewal against the broker contract | |
| AWS Secrets Manager | `src/bernstein/core/security/secrets_broker.py` — `AwsSecretsManagerBackend` | | |
| GCP Secret Manager | `src/bernstein/core/security/secrets_broker.py` — `GcpSecretManagerBackend` | | |
| macOS Keychain | `src/bernstein/core/security/secrets_broker.py` — `MacosKeychainBackend` | | |
| Linux keyring | `src/bernstein/core/security/secrets_broker.py` — `LinuxKeyringBackend` | | |
| Azure Key Vault | | | No backend and no open issue. The broker takes a backend in about eighty lines — see the five above — so this is a gap anyone can close, not a boundary. |
| 1Password | | | As above. The `external` backend already shells out to an arbitrary command, which covers the `op` CLI today without a dedicated module. |

## Workload identity

| Target | Shipped | Open | Not planned |
| --- | --- | --- | --- |
| SPIFFE / SPIRE | `src/bernstein/core/identity/spiffe/workload_api.py` — Workload API, SVIDs, IDs | [#5030](https://github.com/sipyourdrink-ltd/bernstein/issues/5030) — bind a token to a proof of possession | |
| mTLS | `src/bernstein/core/identity/spiffe/mtls.py` | | |

## Telemetry and lineage

| Target | Shipped | Open | Not planned |
| --- | --- | --- | --- |
| OpenTelemetry | `src/bernstein/core/observability/otel_bridge.py` — spans; `otel_projection.py` projects the audit chain | | |
| OpenLineage | `src/bernstein/core/persistence/lineage.py`; `bernstein lineage export` | [#4914](https://github.com/sipyourdrink-ltd/bernstein/issues/4914) — emit into a lineage stack an operator already runs | |

## Agent protocols

| Target | Shipped | Open | Not planned |
| --- | --- | --- | --- |
| MCP | `src/bernstein/mcp/` — server, OAuth, approval gate, cost meter | | |
| A2A | `src/bernstein/cli/commands/a2a_cmd.py`; agent cards under `core/identity/` | | |

## Inference endpoints

Adapters live under `src/bernstein/adapters/`, one module per CLI, and the
[adapter index](adapters/index.md) is the list. Hosted and self-hosted
endpoints are reached through whichever adapter you run rather than through a
provider integration here, which is why they are not enumerated in this table —
adding a provider is an adapter change, not a change to this page.

## Not on this page, deliberately

- **Comparisons with other projects.** This page says what we connect to. It
  never says what anyone else does or does not.
- **Aspirational rows.** Not shipped and no issue means no row. If you want
  something here, the way in is to open the issue — then it has a cell.
