# Compatibility

This page describes practical compatibility boundaries for Bernstein integrations.

Last updated: 2026-04-01

---

## Runtime compatibility

- Python: project targets Python 3.12+.
- Task server/API: FastAPI-based local or remote server operation.
- CLI adapters: multiple agent CLIs supported through adapter abstraction.

Compatibility details can vary by adapter version and local toolchain.

---

## Protocol and integration layers

### MCP

- Bernstein includes MCP server support in `src/bernstein/mcp/server.py`.
- CLI exposes `bernstein mcp`.
- Practical compatibility depends on client/runtime transport expectations.

### A2A

- A2A task/artifact routes are implemented in task routes.
- A2A is available as part of the server API surface.

### ACP

- ACP-related compatibility workflows/spec docs exist.
- Treat ACP support as integration-dependent rather than one fixed matrix.

---

## How to verify in your environment

Use environment-specific validation instead of relying on static matrices:

1. Run `bernstein doctor`.
2. Run your target CLI adapter smoke checks (`bernstein test-adapter <name>`).
3. Validate required API endpoints (`/status`, `/tasks`, `/metrics`, protocol-specific routes).
4. If using remote workers, validate cluster endpoints and auth paths.

---

## Notes on historical matrices

Older protocol matrices in docs/workflows are useful as references for prior CI checks, but they should not be treated as evergreen compatibility guarantees for all environments.