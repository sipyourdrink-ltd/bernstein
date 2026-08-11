# Vendored Agent Plugins schemas

JSON Schemas for the open Agent Plugins specification (agent-plugins.org),
vendored verbatim so manifest validation never fetches anything at load
time (air-gap deploys validate offline against these exact bytes).

| File | Validates |
|---|---|
| `1.0.0/plugin.schema.json` | root `plugin.json` |
| `1.0.0/mcp.schema.json` | root `mcp.json` |

- Source: https://github.com/agentplugins/agent-plugins-spec (`schemas/1.0.0/`)
- Spec version: 1.0.0
- Retrieved: 2026-08-10 (upstream commit `bd383552095128f6effe895b9257cfd580a6d179`)

Do not hand-edit these files. To move to a newer spec version, vendor it
as a new `schemas/agent-plugins/<version>/` directory and update the
generator and tests to pin it explicitly.
