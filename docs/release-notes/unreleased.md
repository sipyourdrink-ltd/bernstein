# Unreleased

Changes merged to `main` that are not yet part of a tagged release. Each
tagged release has its own page in this directory; this page carries what has
landed since the newest one.

Cutting a version empties this page: every entry the tag ships moves onto that
version's page in the release PR itself. `tests/unit/test_unreleased_notes_rotation.py`
holds the page to that — an entry naming an issue or PR a tagged release page
already documents fails the build. An entry that cites released work as context
rather than as its own attribution is exempted by hand there, with the reason.

## Added

- `bernstein run` on a TTY now says it is waiting for the first agent instead of sitting silent (#4257). The wait is bounded and returns as soon as an agent registers, so a fast start stays exactly as quiet as before: the transient status appears only once a poll has already failed to produce a verdict, and clears before the dashboard or the Rich fallback renders. The non-interactive detach path is unchanged.

## Fixed

- The PostgreSQL backend ignored the claiming agent's role in `claim_batch`: the parameter was accepted but never reached either UPDATE (#4323), which filtered on id and `status = 'open'` only. A worker registered as one role could therefore claim another role's tasks in a batch, while the in-memory store rejected exactly that claim and `claim_next` gated on role in its own SQL. The role predicate is now folded into the statement the same way the tenant scope already was, so the check is atomic with the claim on both backends.
- `claim_by_id` on the PostgreSQL backend ignored the claiming agent's role the same way `claim_batch` did (#4325): the parameter never reached `_claim_row`, and neither claim statement carried a role predicate, so a worker could claim a single task belonging to another role. The gate now rides in both the CAS and non-CAS statements. A mismatch raises the same error the in-memory store raises - one wording, now owned in one place - and the CAS path reports it as a role mismatch rather than a version conflict, so the caller does not retry a claim that cannot succeed.
- Claiming a task over HTTP failed on the PostgreSQL backend (#4328). Both routes pass `claimed_by_session`, the store accepted no such parameter, and the resulting `TypeError` was not caught by the handlers around the call - so `POST /tasks/{id}/claim` and `POST /tasks/claim-batch` both answered 500 there. The parameter is now part of the store contract, and the PostgreSQL store records the owning session in the same statement that flips the status, so a claimed task always carries its owner. The column is added idempotently at startup, so existing installs pick it up without manual DDL. A conformance test now pins both stores to the contract's claim signatures, which is what would have caught this before it shipped.
- The PostgreSQL store never recorded which tenant a task belonged to, and queried a `tenant_id` column its own schema never created (#4332). Because the claim route always resolves a scope - `"default"` on a single-tenant install - every batch claim on that backend hit the missing column, and a task created under a tenant was stored with no scope at all, so tenant isolation was not enforced there. The column is now part of the schema, added idempotently at startup, and the tenant is persisted on create and read back on load. **Tasks written before this release carry no recorded tenant and land in the default scope**; on a multi-tenant PostgreSQL install their original scope is not recoverable from the database.
- Creating a task on the PostgreSQL backend raised `ImportError` (#4333). `create()` imported a helper from `bernstein.core.server`, which has not exposed that name since it became a package; the import sat inside the method, so nothing failed until a task was actually created. It now comes from the module that owns it, and `create()` is covered by a test on this backend for the first time.
- The `bernstein` MCP bridge injected into every agent spawn pointed at `python -m bernstein.mcp.server`, a module with no `__main__` guard: running it imported the module and exited 0 without ever answering the MCP handshake (#4313). Every spawned agent's init event showed the `bernstein` server as disconnected and never received the bridge tools (`bernstein_post_message`, `bernstein_claim`, `bernstein_complete`, and the rest), so anything downstream that depends on agent-posted progress signals starved silently. The spawn spec now points at the package's own stdio entrypoint, `python -m bernstein.mcp` (`bernstein/mcp/__main__.py` -> `run_stdio()`), and `bernstein/mcp/server.py` gained a `__main__` guard calling the same `run_stdio()` directly, so the previously-specced module path answers too.
