# MCP Server Injection

A Bernstein plugin can inject Model Context Protocol (MCP) server
definitions into every spawned agent's MCP config. The `provide_mcp_servers`
plugin hook lets your distribution ship - for example - a Postgres MCP
server, an internal-knowledge-base MCP, a credentials vault MCP, or a
filesystem MCP scoped to the agent's worktree, **without** asking each
operator to edit `bernstein.yaml`.

This page covers the hook contract, the merge flow at spawn time,
security considerations, and a worked example.

---

## Why this hook is powerful (and dangerous)

A plugin that injects an MCP server effectively **grants tool access to
every agent in every run that loads the plugin**. Servers run with the
spawning user's privileges; agents discover and call them based on
`keywords` and `capabilities`; a misbehaving server can corrupt the
worktree or leak secrets. Treat the hook as a privileged extension point.

---

## Hook signature

Defined in `src/bernstein/plugins/hookspecs.py`:

```python
@hookspec
def provide_mcp_servers(self) -> list[dict[str, Any]] | None:
    """Return MCP server definitions to inject into agent configs."""
```

A plugin that raises is logged and skipped - it does not crash
collection (`plugins/manager.py`).

### Server-dict fields

| Field | Type | Default | Purpose |
|-------|------|---------|---------|
| `name` | `str` | **required** | Server key (auto-prefixed with the plugin name) |
| `package` | `str` | `""` | npm package installed via `npx -y <package>` |
| `command` | `str` | `"npx"` | Executable override |
| `args` | `list[str]` | `["-y", <package>]` | Argument list override |
| `env_required` | `list[str]` | `[]` | Required env-var names (forwarded into the agent's env) |
| `capabilities` | `list[str]` | `[]` | Tags for capability-based routing |
| `keywords` | `list[str]` | `[]` | Phrases in a task description that auto-attach the server |

---

## What gets injected & when

```text
Plugin loading (process startup)
   │
   ├── PluginManager._safe_call("provide_mcp_servers", ...)
   │     iterates registered plugins
   │
   └── PluginManager.collect_plugin_mcp_servers(registry)      [manager.py]
         │
         └── for each plugin with provide_mcp_servers:
               raw_servers = plugin.provide_mcp_servers()
               for raw in raw_servers:
                   entry = _mcp_entry_from_dict(raw)            [manager.py]
                   entries.append(entry)
               registry.register_plugin_servers(plugin_name,    [mcp_registry.py]
                                                 entries)
                  ↑ sets entry.plugin_name → namespaced_name
                    becomes "<plugin>__<name>"

Agent spawn (per task batch)
   │
   ├── effective_mcp = base_mcp_config (from bernstein.yaml)    [spawner_core.py]
   │
   ├── if mcp_registry is not None:                             [spawner_core.py]
   │     effective_mcp = registry.resolve_for_tasks(tasks,
   │                                                base_config=effective_mcp)
   │     ↑ keywords + capabilities decide which servers attach
   │
   └── if mcp_manager is not None:                              [spawner_core.py]
         effective_mcp = mcp_manager.build_mcp_config_for_task(
             task_mcp_servers=task.mcp_servers,
             base_config=effective_mcp,
         )
         ↑ task-requested servers (task.mcp_servers field) are
           layered on top of plugin and base config

       validate_mcp_readiness(...)                              [spawner_core.py]
       spawn the agent with the merged mcpServers JSON
```

The merge precedence (highest first): `task.mcp_servers` →
`base_config` from `bernstein.yaml` and `~/.claude/mcp.json` →
auto-detected (plugin-provided + catalog) servers.

In the merged JSON, your plugin's server appears under
`mcpServers["<plugin_name>__<server_name>"]`.

---

## Worked example

A minimal plugin that injects a filesystem MCP server scoped to a
specific path.

`acme_fs/__init__.py`:

```python
from bernstein.plugins import hookimpl


class AcmeFilesystemPlugin:
    """Inject a filesystem MCP server scoped to /workspace/shared."""

    @hookimpl
    def provide_mcp_servers(self) -> list[dict[str, object]]:
        return [
            {
                "name": "shared-fs",
                "package": "@modelcontextprotocol/server-filesystem",
                "args": [
                    "-y",
                    "@modelcontextprotocol/server-filesystem",
                    "/workspace/shared",  # least-privilege: one path
                ],
                "capabilities": ["filesystem", "read", "write"],
                "keywords": ["shared assets", "/workspace/shared"],
                "env_required": [],  # filesystem MCP needs no secrets
            }
        ]
```

`acme_fs/pyproject.toml`:

```toml
[project]
name = "acme-fs-plugin"
version = "0.1.0"

[project.entry-points."bernstein.plugins"]
acme = "acme_fs:AcmeFilesystemPlugin"
```

After `pip install acme-fs-plugin`, every agent spawn receives a merged
config containing `mcpServers["acme__shared-fs"]` pointing at the
filesystem MCP. A task whose description mentions "shared assets" gets
the server auto-attached via `MCPRegistry.detect_servers()` keyword match
even when the operator did not list it in `task.mcp_servers`.

---

## Security considerations

- **Least privilege.** Scope filesystem MCPs to the smallest path. Never
  pass `/` or the operator's home dir.
- **Env-var hygiene.** List every secret your server consumes in
  `env_required`; the orchestrator copies only listed vars into the
  agent's env (`MCPServerEntry.to_mcp_config()` -
  `core/protocols/mcp/mcp_registry.py`).
- **Fail closed.** A raise in `provide_mcp_servers()` is logged and
  skipped (`plugins/manager.py`). A server that does not start
  is caught by `validate_mcp_readiness()` at spawn
  (`spawner_core.py`) - the spawn warns but does not crash.
- **Plugin policy gates registration.** Bernstein's enterprise plugin
  policy (`plugins_core.policy`) can deny-list your plugin; MCP injection
  requires a registered, allowed plugin - there is no side-channel.
- **Document capabilities.** An MCP server that can run shell commands
  is effectively an RCE vector for the LLM. Be explicit.

---

## Testing your plugin

### Unit test - namespacing

`tests/unit/test_plugins.py` shows the established pattern:

```python
def test_plugin_servers_are_namespaced():
    pm = PluginManager()
    pm.register(_MyPlugin(), name="acme")

    registry = MCPRegistry(config_path=None)
    pm.collect_plugin_mcp_servers(registry)

    config = registry.build_mcp_config(registry.servers)
    assert "acme__shared-fs" in config["mcpServers"]
```

### Integration check - observe the merged config

Spawn one task in dry-run mode, then ripgrep the merged MCP config the
spawner emitted into the worktree:

```bash
bernstein run --dry-run -t "use the shared fs"
rg --no-heading "acme__shared-fs" .sdd/worktrees/*/mcp.json
```

A match confirms hook return → registration → keyword resolution →
spawn-time write all line up.

---

## Code pointers

| Concern | File | Symbol / line |
|---------|------|---------------|
| Hook spec | `src/bernstein/plugins/hookspecs.py` | `provide_mcp_servers` |
| Plugin-side dispatch | `src/bernstein/plugins/manager.py` | `collect_plugin_mcp_servers` |
| Dict → entry conversion | `src/bernstein/plugins/manager.py` | `_mcp_entry_from_dict` |
| Per-plugin registration & namespacing | `src/bernstein/plugins/manager.py` | `_register_plugin_mcp_servers` |
| `MCPServerEntry` dataclass | `src/bernstein/core/protocols/mcp/mcp_registry.py` | `MCPServerEntry` |
| `namespaced_name` property | `src/bernstein/core/protocols/mcp/mcp_registry.py` | `namespaced_name` |
| `register_plugin_servers` | `src/bernstein/core/protocols/mcp/mcp_registry.py` | - |
| Per-task config build | `src/bernstein/core/protocols/mcp/mcp_registry.py` | `resolve_for_tasks` |
| Spawn-time merge & readiness probe | `src/bernstein/core/agents/spawner_core.py` | - |
| Orchestrator-side registry construction | `src/bernstein/core/orchestration/orchestrator.py` | - |
| Test suite | `tests/unit/test_plugins.py` | `_MCPServerPlugin`, collision test -, error-isolation - |

## Related

- `integrations/plugin-sdk.md` - full plugin authoring guide.
- `integrations/hook-system.md` - wider hook lifecycle.
- `architecture/state-persistence.md` - where the merged MCP config
  lands on disk.
