# Deferred adapters

This page tracks coding/agent CLIs that we've evaluated and chosen NOT
to integrate as Bernstein adapters, with the reasons recorded so the
next person who has the idea doesn't redo the research from scratch.

The default verdict for any new agent is "ship it" - short-lived
agent + CLI surface + clear stdout = adapter in 80-150 lines. We add
to this list only when there's a structural reason the adapter contract
doesn't fit.

Verdicts are dated. Re-evaluate when the listed condition changes.

## Verdict reference

- **DEFER** - track the project; revisit if it ships a stable CLI.
- **SKIP** - structural reason this won't fit Bernstein's spawn model
  (web-only, in-IDE-only, gated behind a vendor account with no
  programmatic surface).

---

## SAP Joule for Developers - SKIP (2026-05-06)

"SAP Joule for Developers" is an umbrella for design-time AI baked
into SAP Build Code, SAP Build Apps, SAP Build Process Automation,
and the ABAP environment in BTP / S/4HANA Public Cloud. There is no
`joule` binary, no `sap-joule exec`, no documented headless
invocation. Joule is invoked from inside the SAP Build Code IDE or
the Joule Studio web UI - neither of which fits Bernstein's
spawn-and-exit model.

A "Pro-code Development Tools for Joule" runtime API does exist, but
it is BTP-gated: OAuth 2.0 ROPC against SAP Cloud Identity Services
plus client-certificate auth for ABAP environments, all behind a paid
SAP BTP subaccount + entitlement. There is no free or community tier
and no "log in with GitHub" path. SAP itself recommends the inverse
integration (external agents calling Joule via SAP Cloud Connector +
AI Hub), which belongs in a future MCP/skills ticket, not adapter
code.

Re-evaluate when:

- A standalone `joule-cli` binary ships, OR
- A paying SAP customer asks for it.

## SAP Joule integration via MCP (skills) - TODO not adapter

The SAP-recommended integration shape is "external agent calls Joule
Skills as remote tools" via SAP Cloud Connector + AI Hub. Bernstein
agents (Claude Code, Codex, Gemini CLI) reaching Joule that way is
fundamentally an MCP/skills concern, not a CLIAdapter concern. When
real demand surfaces, the venue is a new MCP catalog entry under
`docs/integrations/` and a skill pack - not a new file under
`src/bernstein/adapters/`.

## Tessl Framework - SKIP (2026-05-06)

The `tessl` CLI exists, but under the hood it runs `claude-code`,
`codex`, or `cursor` to do the actual work - it is a spec installer /
agent harness wrapper. Wrapping it as a Bernstein adapter would mean
we wrap a wrapper: the adapter would inherit whatever process model
Tessl uses to drive its underlying agent, which breaks the
spawn-and-exit contract's assumption of a single observable process.

If integration ever happens, the right venue is the planning layer:
ingest a Tessl spec as input to a Bernstein plan, then let our own
adapters execute. Re-evaluate if Tessl ships a first-class agent
that does inference on its own rather than delegating to
claude-code/codex/cursor.

## Tabby (TabbyML) - DEFER (2026-05-06)

Tabby is a self-hosted local server (23k+ stars) that exposes
completion and chat over HTTP. There is no agentic CLI - no
short-lived process to spawn, no stdout to harvest, no exit code
to interpret as task success/failure. An adapter would have to
drive it as an HTTP client, which is a category Bernstein's
adapter contract does not currently model (every other adapter is
process-spawn).

Adding HTTP-driven adapters is possible, but it's an architecture
exception we'd only take on for a project with clear pull. Re-evaluate
when there is demand from at least three distinct users, or when
TabbyML ships a true CLI agent that fits the spawn-and-exit model.

## Suna (Kortix) - DEFER (2026-05-06)

Suna (Kortix, 14k+ stars, Apache 2.0) ships a `kortix` CLI with
`start / stop / logs / status` subcommands - a service-manager
surface, not a short-lived task process. It is also a generalist
agent platform (browser, files, web crawl) rather than a
coding-focused CLI, and the runtime is Docker-based. There is no
single one-shot invocation whose stdout and exit code map onto task
success or failure, which is what the spawn-and-exit adapter
contract needs.

Re-evaluate when Suna ships a headless, coding-focused one-shot
execution mode that fits the spawn-and-exit model.

## DeepSeek CLI - DEFER (2026-05-06)

There are multiple community CLIs in this space (`deepseek-cli`,
`deep-code`, others) that all wrap DeepSeek API endpoints, but no
canonical first-party `deepseek` binary. Picking one community fork
to support means picking sides; users on a different fork would get
broken behaviour, and we'd inherit maintenance risk for code that
isn't ours.

DeepSeek models are already reachable through the `openai_agents`
runner (served natively via the DeepSeek API endpoint), `aider`, or
`ollama`, all of which work today, so an adapter would not add a
capability the spawn-and-exit contract does not already cover.
Re-evaluate when DeepSeek ships an official CLI from the project
itself.

## Phind / Pieces / Sweep - SKIP (2026-05-06)

All three are IDE-only or web-only. Phind ships a chat surface in
VS Code; Pieces is an in-IDE memory/snippet tool; Sweep operates as
a GitHub bot rather than a local CLI. None has an agentic CLI
suitable for the short-lived spawn-and-exit model Bernstein assumes.

There is no realistic adapter path here. Do not re-evaluate unless
the underlying product changes shape - e.g. one of them ships a
headless `phind exec` / `pieces run` / `sweep run-local` binary
with non-interactive output.

## Vercel v0 / Lovable / Bolt.new - SKIP (2026-05-06)

These are web-only build-an-app surfaces aimed at UI generation, not
general coding agents. v0 has shipped an MCP server, but no agentic
CLI. Bernstein already supports MCP servers as tools, so v0
specifically is reachable today through the existing MCP catalog
path - no adapter needed.

Lovable and Bolt.new have not shipped programmatic surfaces at all.
Re-evaluate any of them when a true CLI agent ships, but UI-led
product positioning makes this unlikely.

---

## Adding a new entry

If you've evaluated a coding/agent CLI and decided not to ship an
adapter, add a section above with:

- **Project name** + **verdict tag** + **YYYY-MM-DD**
- One paragraph on what it is and why it doesn't fit
- "Re-evaluate when:" condition that would flip the verdict

The point of this file is reversibility: every "no" should have a
clear condition under which it becomes a "yes".
