# Eventing v2: grammar, feed, triggers, and receipts

Bernstein records what happens across several subsystems: the SSE stream, the
`TriggerEvent` sources, the HMAC-chained audit log, the per-step journal, and the
telemetry taxonomy. Eventing v2 unifies them behind one canonical grammar that is
a deterministic projection over the audit chain, so the thing you query, alert
on, and act from is the chain itself rather than a sixth event system.

Source of truth: `src/bernstein/core/events/`.

## The canonical grammar

A canonical event is a pure projection of one audit-chain entry:

| Field                  | Meaning |
|------------------------|---------|
| `hmac`                 | The audit entry HMAC. This is the event's chain-position identity. |
| `prev_hmac`            | The predecessor entry HMAC. Completeness and order verify against this. |
| `resource_id`          | The primary resource the event concerns. |
| `label`                | Dot-delimited label path, for example `task.completed` or `gate.result`. |
| `related_resource_ids` | Sorted lineage ancestor ids: the edges the sequence layer walks. |
| `payload_digest`       | `sha256:` digest of the entry's `details`. |

The projection is a pure function of the entry bytes. Two hosts holding the same
on-disk chain bytes project byte-identical canonical events, because every field
is either copied from the entry or a deterministic hash of the entry's own bytes.

## Querying the feed

```
bernstein events query --from <hmac> --to <hmac> -o window.json
```

The response is a canonical JSON envelope that embeds the from/to HMAC
fence-posts of the underlying chain slice plus the projected events. Because the
serialisation is canonical (sorted keys, minimal separators, UTF-8), the output
is byte-identical across hosts for the same window and can be hashed, diffed, or
shipped to a downstream verifier.

## Offline verification: completeness and order

```
bernstein events verify window.json
```

`verify` needs no signing key. It checks the window's internal `prev_hmac`
linkage against the embedded fence-posts:

- completeness: the last event's `hmac` equals the upper fence-post, and either
  the first event's `hmac` equals the lower fence-post (a bounded slice) or the
  window is genesis-anchored - its lower fence-post is `null` and the first
  event's `prev_hmac` must equal the genesis HMAC. A genesis-anchored window
  therefore cannot silently drop its leading event;
- order: every event's `prev_hmac` equals its predecessor's `hmac`;
- identity: no two events share an `hmac`, and no event links to itself
  (`hmac == prev_hmac`), so a forged cycle cannot pass as a contiguous chain.

Deleting, inserting, or reordering any single event inside the window breaks the
linkage or moves a fence-post and fails the check. The CLI additionally rejects a
window file that is not a well-formed envelope (missing `from_hmac`/`to_hmac`, or
whose `events` is not a list of objects), so dropping a fence-post to disable a
bound is caught at the boundary. "What happened, in what order" settles by chain
position, not by timestamps scattered across five files.

## Composable trigger semantics

Every evaluator is a pure fold over a projected window, so a fire decision can be
re-derived from a chain slice alone.

- **Threshold**: N matching events within a window (a span of chain positions),
  with `for_each` bucketing on grammar labels (`resource_id`, `actor`, `label`).
  Example: three gate failures on one adapter version within ten positions.
- **Absence**: after event A, expect event B within N positions, else a violation
  whose bounds are two named chain positions. This is the negative-proof input.
  A violation is only asserted once the deadline is *observed* (the slice reaches
  `A.position + N`); an anchor whose deadline the slice has not yet reached is
  deferred, so the negative proof is never asserted on an incomplete window.
- **Sequence**: an ordered pair keyed on lineage descent, not wall-clock order.
  The later event fires only when it provably descends from the earlier one
  through the `related_resource_ids` edges. Two unrelated events in the right
  wall-clock order never fire.
- **Compound**: any / all / count over a set of label conditions.

## Receipt-backed actions

Rules dispatch typed actions (notify, pause or resume a schedule, cancel or
suspend a task, clamp a budget envelope, drain the warm pool, pin an adapter to
its last green version, force a checkpoint-fork retry). Automation runs under the
same receipt discipline as everything else:

1. A rule fire mints a **fire receipt** on the chain (rule hash, matched event
   HMACs, rendered action digest) **before** the effect executes.
2. The effect runs only if the receipt authorises exactly that rendered action;
   an executed action whose digest no receipt commits to is rejected.
3. The executed action is itself a chain event, referencing its fire receipt and
   the triggering event, so automation is observable in the same feed it reacts
   to.

The same rule against the same event bytes renders a byte-identical action, so
the receipt's commitment is reproducible by any verifier.

An expired absence expectation writes an **expectation-expired** receipt carrying
a negative proof: no event matching a label exists between two named chain
positions. A verifier confirms it against the stored window; injecting a matching
event into the window makes the receipt fail.

## Webhook payload templates

The generic inbound webhook path accepts a new external system's POST through
configuration instead of code. A declarative template maps JSON payload paths to
canonical event fields. The discipline is content-addressing before rendering:
the raw payload bytes are hashed into the chain first, so a render is always
reproducible from the recorded bytes, and a render failure emits a diagnostic
feed event carrying only the payload digest, never the payload.

The `resource_path` must resolve to a non-empty scalar (a string or integer,
never a bool, object, or array). A non-scalar fails the render with
`invalid_resource` rather than serialising the payload subtree into
`resource_id`, so a nested payload can never leak into the feed.

## Compatibility

Eventing v2 is additive. Existing SSE consumers and existing `triggers.yaml`
configs work unchanged, and the create-task action path produces identical
results for existing configs. Counter and expectation state lives per project
under `.sdd/runtime/triggers/`, isolated across concurrent workers.
