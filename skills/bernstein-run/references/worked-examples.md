# Worked examples

The skill body stays host-neutral: every host ultimately shells out to the
same `bernstein` CLI against the same install, so one Bernstein workspace
serves every agent session that can run shell commands.

## End-to-end transcript

```bash
# 1. Submit
bernstein run -g "add retry with backoff to the payment client and cover it with tests"

# 2. Monitor until tasks settle
bernstein status
bernstein cost

# 3. Verify the finished run (run id is printed by `bernstein run` / `status`)
bernstein lineage verify run-2026-07-11-a1b2
bernstein audit verify
```

A goal is done when step 3 exits 0 twice. A non-zero `lineage verify`
means the run journal is empty or tampered; a non-zero `audit verify`
means the workspace audit chain does not check out. Surface either
verbatim instead of summarising around it.

## Host notes

### Claude Code

- Install the plugin (bundles this skill, slash commands, and MCP server
  registration), or copy this skill directory into `.claude/skills/`.
- With the plugin: `/bernstein:run <goal>`, `/bernstein:status`,
  `/bernstein:stop`.
- With MCP registered: `bernstein_run`, `bernstein_status`,
  `bernstein_cost`, `bernstein_stop`, `bernstein_approve` tools.

### Codex CLI

- Copy this skill directory into `.codex/skills/` (project) or
  `~/.codex/skills/` (user), or drive the CLI directly from shell.

### Gemini CLI

- Copy this skill directory into `.gemini/skills/` or invoke the CLI
  directly; `bernstein mcp` also serves the tools over MCP stdio.

### Copilot CLI

- Copy this skill directory into `.github/skills/` (project) or
  `~/.copilot/skills/` (user).

### Cursor

- Copy this skill directory into `.cursor/skills/`, or install the Cursor
  plugin under `packages/cursor-plugin/`.

If a host scans a different directory, install with an explicit
destination:

```bash
bernstein skills package install --dest <host-skills-dir>/bernstein-run
```

## Receipt-backed install

Every install path above can be anchored to the workspace audit chain:

```bash
# Copy the bundled skill into a host directory and anchor a receipt
bernstein skills package install --host claude --scope project

# Anchor a tree the host already installed (e.g. a plugin checkout)
bernstein skills package install --dest <installed-dir> --record-only

# Later: prove the tree still matches what was anchored
bernstein skills package verify --dest <installed-dir>
```
