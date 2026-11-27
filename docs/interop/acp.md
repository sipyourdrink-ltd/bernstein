# ACP: server and client roles

Bernstein speaks the [Agent Client Protocol](https://agentclientprotocol.org)
(**ACP v1**, `protocolVersion: 1`), an open JSON-RPC 2.0 specification, in
**both directions**.

| Role | Command / surface | Direction | What it does |
|------|-------------------|-----------|--------------|
| Server | `bernstein acp serve --stdio` | IDE -> Bernstein | Exposes Bernstein as an ACP v1 backend over stdio or HTTP/SSE so an editor can drive it. See [ACP bridge](../reference/acp-bridge.md). |
| Client | `EventChannel.ACP` adapter | upstream CLI -> Bernstein | Consumes ACP from an upstream CLI as a first-class adapter event transport. |

> **Legacy REST surface:** The FastAPI REST routes in
> `src/bernstein/core/routes/acp.py` (`/.well-known/acp.json`, `/acp/v0/*`)
> are a separate legacy REST v0 surface and do not carry ACP v1 JSON-RPC.
>
> **Server runner placeholders:** The ACP v1 wire schema, handshake,
> bidirectional transport, and session lifecycle are implemented; embedded
> runner execution, `EventJournal` -> `session/update` projection, live
> approval-gate wiring, and `session/load` replay are tracked in
> [ACP bridge](../reference/acp-bridge.md#planned-in-follow-up-runner-integration-pr-2-placeholders).

This page documents the **client** role: Bernstein consuming ACP from an
upstream CLI, so an adapter receives typed lifecycle events with no bespoke
stdout parser.

---

## Why a client-side transport

Most adapters read lifecycle signals from stdout, either a stream-json dialect
or the canonical `BERNSTEIN:<KIND>` text grammar. Each is a bespoke parsing
path, and each is pinned by golden transcripts that pin text an upstream CLI
never promised to keep stable. When the CLI changes its output format the
parser drifts, which is the leading cause of adapter breakage.

For a CLI that can speak ACP, that failure class is avoidable. The adapter
declares `EventChannel.ACP` and Bernstein reads typed JSON-RPC lifecycle events
directly, with no text parser in the path.

---

## How it works

The upstream CLI is the ACP *agent*; Bernstein is the *client*. Bernstein
supports both ACP v1 (`protocolVersion: 1`, `session/new`, `session/prompt`,
`session/update`, `session/request_permission`) and legacy `"2025-04-01"`
fixtures (`prompt`, `streamUpdate`, `requestPermission`) so existing adapter
recordings stay dual-compatible until upstream CLIs finish migrating.

1. **Spawn.** The CLI is launched as a subprocess speaking line-delimited
   JSON-RPC on stdout (`bernstein.adapters.acp_channel.spawn_acp_subprocess`).
2. **Validate.** Every inbound frame passes through the same schema validators
   the server uses (`validate_request` / `validate_response` in
   `bernstein.core.protocols.acp.schema`). A malformed frame is refused at the
   boundary with a typed `ACPSchemaError` and journals nothing.
3. **Journal, content-addressed.** Each validated event lands in the run's
   Merkle-chained `EventJournal` with the SHA-256 of its canonical bytes
   (`bernstein.core.protocols.acp.client.ACPEventJournalSink`).
4. **Terminal.** The lifecycle ends on the structured `stopReason`, never on
   stdout text.

---

## Replay stability

Because every event is content-addressed, the run's replay identity covers the
agent's output:

- **Byte-identical replay.** Two faithful replays of a recorded ACP session
  produce identical per-step content hashes and chain to the same Merkle head
  (`replay_acp_content_hashes`).
- **Named divergence.** A mutated recorded event surfaces as a hash divergence
  naming the exact step (`compare_acp_journals`), rather than a silent drift a
  re-parse would miss.

This is the load-bearing difference from an ordinary parser swap: strip the
content-addressed journal and the guarantee is gone. An upstream release that
changes only human-readable text inside a `session/update` (or legacy
`streamUpdate`) delta leaves the structured lifecycle - and therefore the
replay - unchanged.

---

## Conformance

For an ACP adapter, lifecycle conformance is an **event-schema fixture**: a
recorded JSON-RPC frame sequence under `tests/fixtures/acp/lifecycle/*.jsonl`,
replayed through the same validated, content-addressed transport the adapter
uses at runtime (`replay_acp_event_fixture` in
`bernstein.adapters.conformance`). This replaces the pinned stdout golden
transcript, shrinking the conformance surface to frames the protocol actually
defines.

For the server role, `.github/workflows/ci.yml` runs the `ACP v1 conformance`
job (`tests/integration/acp/test_acp_v1_conformance.py` and
`tests/integration/acp/test_acp_v1_sdk.py`) against live
`bernstein acp serve --stdio` subprocesses.

---

## Declaring an adapter on the ACP channel

Add an `EventChannel.ACP` row in `STRATEGY_MATRIX`
(`src/bernstein/adapters/_contract.py`):

```python
"my_agent": AdapterStrategy(event_channel=EventChannel.ACP),
```

`kilo` and `kimchi`, whose upstream CLIs expose ACP, ship on this channel. See
the [Adapter Guide](../adapters/ADAPTER_GUIDE.md#declaring-acp-as-the-event-channel)
for the full walkthrough.
