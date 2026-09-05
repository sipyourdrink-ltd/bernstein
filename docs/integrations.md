# Integrations

The systems Bernstein bridges to, named the way you name them. Use this page to
answer "does it talk to the thing I already run" without reading the tracker.

Every row cites its evidence. **Shipped** names a module and a test you can
open. **Open** names an issue number. **Not planned** gives the reason in one
line. A row that can cite none of the three is deleted, not softened — the same
rule the compliance packs run under, where a claim with no evidence behind it is
not a weaker claim, it is not a claim.

One column here does not appear on pages like this, and it is the one worth
reading: **Wired** says whether anything in a running system actually calls the
module. A module with a green test suite and no caller is a working feature to
every reader of the source and to nobody else (see
[#5093](https://github.com/sipyourdrink-ltd/bernstein/issues/5093)). Where the
two differ, this page says so.

## Directories and identity providers

| Target | Shipped | Wired | Open | Not planned |
|---|---|---|---|---|
| SCIM (RFC 7643/7644) | `adapters/directory/scim.py` · `tests/unit/adapters/test_directory_scim.py` — turns standard provisioning resources into `PrincipalLedger` calls | yes — registered through `core/security/directory_registry.py` | — | — |
| Generic OIDC | `core/security/sso_oidc.py` · `tests/unit/test_sso_oidc.py` — Authorization Code flow, `.well-known` discovery, PKCE, refresh, group→role mapping | yes — `core/security/rbac.py` | — | — |
| Any directory, as a protocol | `core/security/directory_bridge.py` · `tests/unit/test_directory_bridge.py` — resolve a principal, list group memberships, report a revocation | yes — `rbac.py`, `plugins/hookspecs.py` | — | — |
| Okta | via the OIDC and directory-bridge surfaces above | — | [#5018](https://github.com/sipyourdrink-ltd/bernstein/issues/5018) — bridge agent principals to Okta | — |
| Microsoft Entra ID | via the OIDC and directory-bridge surfaces above | — | [#5018](https://github.com/sipyourdrink-ltd/bernstein/issues/5018) | — |
| LDAP | — | — | — | No issue and no module. The directory bridge is the extension point; an LDAP adapter would implement it. |

## Secret stores

| Target | Shipped | Wired | Open | Not planned |
|---|---|---|---|---|
| HashiCorp Vault | `core/security/vault_injector.py` (`_VaultInjector`) · `tests/unit/test_vault_injector.py` — per-agent credentials injected at spawn, revoked at exit | **no** — see the note below | [#5021](https://github.com/sipyourdrink-ltd/bernstein/issues/5021) — reference backend for the broker | — |
| AWS Secrets Manager | `core/security/vault_injector.py` (`_AwsInjector`) · same suite | **no** | — | — |
| 1Password | `core/security/vault_injector.py` (`_OnePasswordInjector`) · same suite | **no** | — | — |
| Broker in front of a store | `core/security/secrets_broker.py` · `tests/unit/security/test_secrets_broker.py` | yes | [#5021](https://github.com/sipyourdrink-ltd/bernstein/issues/5021) | — |
| Azure Key Vault | — | — | — | No module and no issue. `vault_injector.py`'s three injectors are the shape a fourth would take. |

> **`vault_injector` is not wired.** The module and its tests exist, but the only
> reference to it anywhere in `src/` is an alias entry in `core/__init__.py`'s
> redirect map — no code path calls it. Credential injection at agent spawn is
> therefore implemented and unreachable. This is the exact shape
> [#5100](https://github.com/sipyourdrink-ltd/bernstein/issues/5100) describes,
> where the redirect map makes a module read as reachable to any tool that only
> asks "is this name imported somewhere", and it is tracked in the caller-less
> set on [#5505](https://github.com/sipyourdrink-ltd/bernstein/issues/5505).

## Policy engines

| Target | Shipped | Wired | Open | Not planned |
|---|---|---|---|---|
| Open Policy Agent (Rego) | `core/security/external_policy_hook.py` (`OPAHook`) · `tests/unit/test_external_policy_hook.py` — fires before the permission check; the response overrides the default | yes — `core/security/authzen.py` | [#4912](https://github.com/sipyourdrink-ltd/bernstein/issues/4912) — engines are bridged but the decision path is incomplete | — |
| Cedar | `core/security/external_policy_hook.py` · same suite | yes | [#4912](https://github.com/sipyourdrink-ltd/bernstein/issues/4912) | — |
| AuthZEN 1.0 | request shape in `external_policy_hook.py` (`AuthZenResource`) | partial | [#5032](https://github.com/sipyourdrink-ltd/bernstein/issues/5032) — speak AuthZEN 1.0 at the decision boundary | — |

## Workload identity

| Target | Shipped | Wired | Open | Not planned |
|---|---|---|---|---|
| SPIFFE | `core/identity/spiffe/` — `spiffe_id.py`, `svid.py`, `workload_api.py`, `grant_identity.py`, `binding.py` | yes — `core/identity/agent_registry.py`, `grants.py`, `agent_card.py` | — | — |
| SPIRE | reached through the SPIFFE Workload API above (`workload_api.py`) | yes | — | — |
| mTLS | `core/identity/spiffe/mtls.py` | yes | — | — |

## Telemetry and lineage

| Target | Shipped | Wired | Open | Not planned |
|---|---|---|---|---|
| OpenTelemetry (ingest) | `core/observability/otlp_ingest.py` · `tests/unit/test_otlp_ingest.py` | yes — `cli/commands/governance_cmd.py` | — | — |
| OpenTelemetry (export) | `core/observability/telemetry.py` | yes | — | — |
| OpenLineage | `core/persistence/openlineage_export.py` · `tests/unit/test_openlineage_export.py` | yes — `cli/commands/lineage_export_cmd.py` | — | — |

## Agent protocols

| Target | Shipped | Wired | Open | Not planned |
|---|---|---|---|---|
| MCP | `src/bernstein/mcp/`, `core/protocols/mcp_catalog/`, `core/routes/mcp_bot_tools.py`, `core/protocols/mcp_bot_allowlist.py` | yes | — | — |
| A2A | `core/protocols/a2a/`, `core/routes/a2a_jsonrpc.py`, `core/routes/task_a2a.py` · `tests/unit/test_a2a_receipt_caller.py` | yes | — | — |

## Inference endpoints

| Target | Shipped | Wired | Open | Not planned |
|---|---|---|---|---|
| Hosted and self-hosted endpoints | the adapter layer under `src/bernstein/adapters/` | yes | — | — |
| AWS Bedrock | named only in `adapters/garak.py`, as a scanner target | — | — | No first-class adapter and no issue. Bedrock endpoints are reachable through the generic endpoint configuration; a dedicated adapter is not scheduled. |

## Keeping this page honest

- A row moves to **Shipped** when a module and a test exist, and names both.
- **Wired** is a separate question from shipped, and is answered by whether
  anything outside the module (and outside a redirect or alias table) calls it.
- A capability that is neither shipped nor tracked by an issue does not get a
  row. Aspirational entries are the failure mode this page invites.
- Comparisons with other projects belong nowhere on this page.
