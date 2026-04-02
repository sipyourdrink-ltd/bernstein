# Research: Agent Client Protocol (ACP) for JetBrains Integration

**Task ID:** 7cb5372c4510
**Date:** 2026-03-29
**Status:** Complete
**Research Effort:** 2-3 hours

---

## Executive Summary

The **Agent Client Protocol (ACP)** is an open-standard, JSON-RPC-based protocol for connecting AI coding agents to code editors. It provides a significantly simpler integration path for Bernstein agents than building a custom JetBrains plugin.

**Key Finding:** ACP is **dramatically simpler** than custom plugin development:
- Custom plugin: Requires Kotlin rewrite, IDE-specific APIs, ongoing maintenance (estimated 40-60 hours)
- ACP integration: JSON-RPC wrapper around existing task server, works across editors (estimated 12-16 hours)

---

## Part 1: ACP Protocol Specification

### 1.1 Overview

ACP (Agent Client Protocol) is an open standard for IDE/editor and AI agent communication:
- **Developed by:** Zed Industries (with JetBrains/Zed backing)
- **Current version:** 0.11.4 (35 releases, actively maintained)
- **Architecture:** JSON-RPC 2.0 over stdio (local) or HTTP/WebSocket (remote)
- **Adoption:** 30+ agents registered (Claude, Cline, Cursor, Gemini CLI, Qwen, etc.)
- **License:** Apache 2.0
- **Specification:** [agentclientprotocol.com](https://agentclientprotocol.com)

### 1.2 Core Protocol Messages

ACP uses JSON-RPC 2.0 with the following standardized message types:

#### Initialization Flow
```json
// Client → Agent (session/initialize)
{
  "jsonrpc": "2.0",
  "id": 1,
  "method": "session/initialize",
  "params": {
    "protocolVersion": "0.11.4",
    "clientCapabilities": {
      "terminalAuth": true,  // Support terminal-based auth
      "mcp": true            // Support MCP servers
    }
  }
}

// Agent → Client (response)
{
  "jsonrpc": "2.0",
  "id": 1,
  "result": {
    "agentCapabilities": {
      "auth": {
        "type": "env" | "agent" | "terminal",
        "methods": [...]
      },
      "tools": [...]
    }
  }
}
```

#### Session Management
- **session/new** — Start a new task session
- **session/prompt** — User sends task/input
- **session/update** — Agent streams responses
- **session/cancel** — Stop current task

#### Message Flow Example
```
Editor → Agent:  session/initialize
  ↓
Agent → Editor:  capabilities (auth methods, tools, etc.)
  ↓
Editor → Agent:  session/new (establish connection)
  ↓
Editor → Agent:  session/prompt (task description)
  ↓
Agent → Editor:  session/update (streamed responses)
  ↓
Editor → Agent:  session/cancel (if user stops)
```

### 1.3 Key Protocol Features

| Feature | Detail |
|---------|--------|
| **Transport** | stdio (local), HTTP/WebSocket (remote in development) |
| **Format** | JSON-RPC 2.0 |
| **Authentication** | 3 methods (see section 1.4) |
| **Tools** | MCP-compatible tool descriptions |
| **Artifacts** | Support for file/diff artifacts |
| **Streaming** | Chunked updates via `session/update` |
| **Cancellation** | Graceful `session/cancel` support |

### 1.4 Authentication Methods

ACP defines three authentication approaches:

#### 1. Agent-Handled Authentication (Default)
- Agent manages its own auth flow
- **Backward compatible** with existing agents
- Used by: Claude Code, Cursor
- Pro: No client changes needed
- Con: Client can't render custom UI for credentials

#### 2. Environment Variable Authentication
- Client sets env vars before spawning agent
- Supports metadata: labels, secret flags, optional fields
- Agent declares what variables it needs
- **Used by:** Most registry agents
- Pro: Simple, client can render input forms
- Con: Limited to static environment

#### 3. Terminal Authentication
- Client provides interactive terminal to agent
- Allows agent to prompt user directly (e.g., `npm login`)
- Requires `ClientCapabilities.terminalAuth=true`
- Pro: Supports complex login flows
- Con: Requires client support

**Example: Environment Variable Auth Declaration**
```json
{
  "auth": {
    "type": "env",
    "variables": [
      {
        "name": "OPENAI_API_KEY",
        "description": "OpenAI API key",
        "secret": true,
        "required": true,
        "link": "https://platform.openai.com/api-keys"
      }
    ]
  }
}
```

---

## Part 2: Agent Registration & Discovery

### 2.1 ACP Registry

Agents register at: **[agentclientprotocol/registry](https://github.com/agentclientprotocol/registry)**

Registry URL (auto-updated hourly):
```
https://cdn.agentclientprotocol.com/registry/v1/latest/registry.json
```

### 2.2 Registration Requirements

To register a Bernstein agent, create a PR with:

1. **Directory Structure**
   ```
   bernstein/
   ├── agent.json         (required)
   ├── icon.svg          (16x16, monochrome)
   └── README.md         (optional)
   ```

2. **agent.json Schema**
   ```json
   {
     "id": "bernstein",
     "name": "Bernstein",
     "version": "0.1.0",
     "description": "Multi-agent orchestration for CLI coding agents",
     "repository": "https://github.com/user/bernstein",
     "website": "https://bernstein.dev",
     "authors": ["Your Name"],
     "license": "MIT",
     "distribution": {
       "npm": "@user/bernstein@0.1.0",
       "pypi": "bernstein==0.1.0",
       "binary": {
         "darwin-arm64": "https://...",
         "darwin-x64": "https://...",
         "linux-x64": "https://...",
         "windows-x64": "https://..."
       }
     }
   }
   ```

3. **Authentication Support** (Required)
   - Agent must declare supported auth methods
   - CI automatically validates via ACP handshake
   - All 30+ current registry agents support authentication

4. **Icon Requirements**
   - 16×16 SVG
   - Monochrome (use `currentColor`)
   - No hardcoded colors

### 2.3 Discovery Flow

```
1. JetBrains IDE fetches registry.json
2. User selects "Bernstein" from agent marketplace
3. IDE downloads distribution (npm/PyPI/binary)
4. IDE spawns agent as subprocess with stdio transport
5. Agent connects back to Bernstein task server via HTTP
6. Editor ↔ Agent ↔ Bernstein Task Server (3-way)
```

---

## Part 3: How Bernstein Agents Would Register & Operate

### 3.1 Integration Architecture

```
┌─────────────────────────────────────────────────────────┐
│                  JetBrains IDE                          │
│  ┌──────────────────────────────────────────────────┐  │
│  │  ACP Session Manager                             │  │
│  │  • spawns bernstein-agent subprocess             │  │
│  │  • manages session/initialize, prompt, update    │  │
│  │  • streams results to editor UI                  │  │
│  └──────────────────────────────────────────────────┘  │
└────────────────┬───────────────────────────────────────┘
                 │ stdio (JSON-RPC)
                 │
┌────────────────▼───────────────────────────────────────┐
│          bernstein-agent CLI                           │
│  ┌──────────────────────────────────────────────────┐  │
│  │  ACP Protocol Handler                            │  │
│  │  • parses session/initialize                     │  │
│  │  • maps session/prompt to Bernstein task         │  │
│  │  • streams task output via session/update        │  │
│  └──────────────────────────────────────────────────┘  │
└────────────────┬───────────────────────────────────────┘
                 │ HTTP
                 │
┌────────────────▼───────────────────────────────────────┐
│       Bernstein Task Server (localhost:8052)           │
│  ┌──────────────────────────────────────────────────┐  │
│  │  POST /tasks — create task from ACP prompt      │  │
│  │  GET /tasks/{id} — poll task status             │  │
│  │  SSE /a2a/tasks/{id}/events — stream results    │  │
│  └──────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────┘
```

### 3.2 Agent Spawn Command

When JetBrains IDE launches a Bernstein agent from registry:

```bash
bernstein-agent \
  --auth-type env \
  --task-server http://localhost:8052 \
  --stdio
```

Or with authentication:

```bash
BERNSTEIN_API_KEY=abc123 \
BERNSTEIN_MODEL=claude-opus \
bernstein-agent --stdio
```

### 3.3 Session Lifecycle

#### Step 1: Initialization
```json
// IDE → Agent (from IDE's ACP handler)
{"jsonrpc": "2.0", "id": 1, "method": "session/initialize",
 "params": {"protocolVersion": "0.11.4"}}

// Agent → IDE
{"jsonrpc": "2.0", "id": 1, "result":
 {"agentCapabilities": {
   "auth": {"type": "env", "methods": ["BERNSTEIN_API_KEY"]},
   "tools": ["spawn-agents", "cost-tracking"]
 }}}
```

#### Step 2: New Session
```json
// IDE → Agent
{"jsonrpc": "2.0", "id": 2, "method": "session/new"}

// Agent → IDE
{"jsonrpc": "2.0", "id": 2, "result": {"sessionId": "sess_123"}}
```

#### Step 3: Prompt
```json
// IDE → Agent (user describes task)
{"jsonrpc": "2.0", "id": 3, "method": "session/prompt",
 "params": {"text": "Write a Python function to calculate Fibonacci"}}

// Agent creates task on task server
// Agent starts polling task status
```

#### Step 4: Streaming Updates
```json
// Agent → IDE (while task runs)
{"jsonrpc": "2.0", "method": "session/update",
 "params": {"text": "Agent A spawned to write function...",
            "token": "continuation_token_123"}}

// IDE displays streaming response in chat panel
```

#### Step 5: Completion
```json
// Agent → IDE (task done)
{"jsonrpc": "2.0", "method": "session/update",
 "params": {"text": "✓ Task complete. Code written to main.py",
            "artifacts": [{"path": "main.py", "type": "file"}]}}
```

---

## Part 4: Integration Effort Assessment

### 4.1 Implementation Scope (Bernstein Side)

| Component | Effort | Notes |
|-----------|--------|-------|
| **ACP Protocol Handler** | 4-6 hours | JSON-RPC message parsing, state machine |
| **Session Manager** | 2-3 hours | Map ACP sessions → Bernstein task IDs |
| **Task Polling Loop** | 1-2 hours | SSE or HTTP polling to task server |
| **Auth Support** | 2-3 hours | Support env var + terminal auth |
| **CLI Entrypoint** | 1-2 hours | `bernstein-agent --stdio` command |
| **Testing** | 2-3 hours | Unit + integration tests |
| **Registry Submission** | 1 hour | agent.json + icon + PR |
| **Documentation** | 1-2 hours | Setup guide, architecture |
| **TOTAL** | **14-22 hours** | Realistic estimate with contingency |

### 4.2 Comparison: ACP vs. Custom JetBrains Plugin

| Aspect | ACP | Custom Plugin |
|--------|-----|---------------|
| **Language** | Python (existing) | Kotlin (new) |
| **Impl. Time** | 14-22 hours | 40-60 hours |
| **IDE Support** | Zed, Neovim, MariaIDE | JetBrains only |
| **Maintenance** | Protocol stable | IDE API changes |
| **Distribution** | npm/PyPI/binary | JetBrains Marketplace |
| **Auth Handling** | Built-in ACP support | Custom UI in Swing |
| **Testing** | Simple (stdio) | IDE SDK setup required |
| **Future-Proofing** | High (open standard) | Medium (vendor-specific) |

### 4.3 Why ACP is Better

1. **Broader Reach**: Works with Zed, Neovim, JetBrains, and future editors without plugin rewrites
2. **Open Standard**: Not locked into JetBrains ecosystem (per their own commitment to openness)
3. **Simpler Development**: JSON-RPC messages vs. IntelliJ Platform SDK complexity
4. **Easier Distribution**: npm/PyPI vs. JetBrains Marketplace approval process
5. **Lower Maintenance**: Protocol is stable and standardized by industry (Zed, JetBrains, etc.)
6. **User Control**: Users can add Bernstein to any ACP-compatible editor without plugin install
7. **No IDE Downtime**: Works with any JetBrains version that supports ACP

---

## Part 5: Authentication Flow Detail

### 5.1 Environment Variable Authentication (Recommended)

```
1. User installs bernstein-agent from registry
2. JetBrains prompts for auth credentials based on agent.json
3. IDE renders:
   - Text field for BERNSTEIN_API_KEY
   - Link to https://bernstein.dev/docs/api-keys
4. User enters API key
5. IDE stores in secure credential manager
6. IDE spawns agent with env var:
   BERNSTEIN_API_KEY=sk_... bernstein-agent --stdio
7. Agent reads BERNSTEIN_API_KEY and connects to task server
8. Agent authenticates HTTP calls: Authorization: Bearer $BERNSTEIN_API_KEY
```

### 5.2 Terminal Authentication (Optional)

If agent needs interactive login:

```json
// Agent declares terminal auth
{"auth": {"type": "terminal"}}

// IDE spawns agent with terminal support
// User can run: bernstein login --interactive
// Agent prompts interactively for credentials
```

---

## Part 6: File-Based Implementation Checklist

To implement ACP support in Bernstein:

### Phase 1: Protocol Handler (6-8 hours)
- [ ] Create `src/bernstein/acp/handler.py` — JSON-RPC message parser
- [ ] Create `src/bernstein/acp/session.py` — ACP session → Bernstein task mapping
- [ ] Create `src/bernstein/acp/auth.py` — Environment variable + terminal auth
- [ ] Implement message types: initialize, new, prompt, update, cancel

### Phase 2: CLI Agent (4-6 hours)
- [ ] Create `src/bernstein/cli/acp_agent.py` — ACP agent entrypoint
- [ ] Implement stdio transport (JSON-RPC over stdin/stdout)
- [ ] Task polling loop with streaming output
- [ ] Artifact handling (file diffs, outputs)

### Phase 3: Integration (2-3 hours)
- [ ] Add ACP handler to `src/bernstein/core/server.py` (optional, for remote support)
- [ ] Create `bernstein-agent` script in setup.py
- [ ] Wire up auth flow (env vars → task server)

### Phase 4: Registry Submission (1-2 hours)
- [ ] Create `registry/bernstein/agent.json`
- [ ] Design 16×16 SVG icon (monochrome)
- [ ] Write README
- [ ] Submit PR to agentclientprotocol/registry

### Phase 5: Testing & Docs (2-3 hours)
- [ ] Unit tests for ACP messages
- [ ] Integration test: IDE ↔ Agent ↔ Task Server
- [ ] Doc: ACP setup guide
- [ ] Doc: Architecture diagram

---

## Part 7: Recommendations

### Immediate Actions

1. **Implement ACP Agent (Week 1)**
   - Priority: Enables JetBrains, Zed, Neovim support without IDE-specific code
   - Effort: 14-22 hours
   - Blocker: None (can work in parallel with other tasks)

2. **Register in ACP Registry (Week 2)**
   - Allows automatic discovery by JetBrains Air users
   - Opens Bernstein to Zed/Neovim communities
   - No ongoing maintenance required

3. **Defer Custom JetBrains Plugin (Q2 2026)**
   - Only build if ACP doesn't meet needs
   - JetBrains Air support is still in preview (macOS only)
   - Plugin API may stabilize in Q2

### Long-Term Positioning

- **ACP as standard**: Bernstein is "IDE-agnostic orchestrator," not vendor-locked
- **Marketing angle**: "Works in JetBrains, Zed, VS Code, CLI — wherever you code"
- **Ecosystem play**: Position as neutral coordinator between editors and agents (similar to Anthropic's stance)

---

## Part 8: Risks & Mitigations

| Risk | Likelihood | Mitigation |
|------|------------|-----------|
| ACP Protocol changes | Low | Protocol is stable (v0.11+); use semantic versioning |
| Auth method complexity | Medium | Start with env vars; add terminal auth later |
| JetBrains IDE update breaks ACP | Very low | JetBrains is ACP founding member |
| Registry approval delays | Low | All agents support auth; clear requirements |
| Remote support incomplete | Medium | Build with local (stdio) first, upgrade later |

---

## Part 9: External References

- **Official Spec**: https://github.com/agentclientprotocol/agent-client-protocol
- **JetBrains Docs**: https://www.jetbrains.com/help/ai-assistant/acp.html
- **Agent Registry**: https://github.com/agentclientprotocol/registry
- **Auth Proposal**: https://agentclientprotocol.com/rfds/auth-methods
- **Blog Post**: https://blog.jetbrains.com/ai/2025/12/agents-protocols-and-why-we-re-not-playing-favorites/
- **Goose Intro**: https://block.github.io/goose/blog/2025/10/24/intro-to-agent-client-protocol-acp/

---

## Conclusion

**ACP is significantly simpler and more valuable than a custom JetBrains plugin.**

The protocol provides:
- ✅ JetBrains IDE integration (via ACP)
- ✅ Zed, Neovim support (bonus)
- ✅ Future editor support (built-in)
- ✅ Open standard (no vendor lock-in)
- ✅ 50% less development effort
- ✅ Broader competitive positioning

**Recommendation**: Build ACP agent first (14-22 hours), register in public registry, defer custom plugin to Q2 2026 if business case emerges.
