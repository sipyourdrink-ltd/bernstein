# x402 settlement for metered MCP gateway calls

When an upstream MCP server answers a proxied tool call with an HTTP **402**
challenge (the x402 pay-and-retry pattern), the gateway can settle the charge
under a signed spending mandate and record a **spend receipt** that ties the
payment to the exact executed invocation it paid for. Bernstein never moves
money itself: the settlement hook is the single boundary where your own payment
tooling plugs in.

The path is **disabled by default**. With no active x402 config a 402 surfaces
as an ordinary tool error - no hook is looked up and no retry is sent.

Module: `src/bernstein/core/protocols/payments/x402.py`. It is the concrete x402
adapter over the mandate / consent-receipt surface documented in
[`spending-mandates.md`](./spending-mandates.md).

## What a settlement proves

A spend receipt is not "a payment plus an audit line". Its identity is a
lineage-spine entry hash, and its bindings recompute offline against two
independent records:

| Binding | Proves |
|---|---|
| WAL invocation record digest | *which executed tool call* the charge paid for |
| 402 challenge digest | the challenge that was answered |
| Payment reference | the (opaque, non-secret) settlement id |
| Retried request digest | the exact request that was replayed after payment |
| Mandate hash | the spend was inside a signed, capped authority |

A payment claim that does not chain to **both** the WAL invocation record and
the authorising mandate fails verification. A provider's statement is therefore
checkable against gateway execution history rather than taken on trust.

## Flow

On a 402 during a proxied `tools/call`, with an active config the gateway:

1. **Gates** the call against the active `IntentMandate` - refusing **fail
   closed** when no mandate authorizes the tool, the spend cap would be
   breached, or the amount cannot be determined. A refusal is itself a
   chain-anchored receipt (`.sdd/x402/refusals/`), so a denial is as provable as
   a payment.
2. **Invokes** the settlement hook *only after* the mandate and cap pass. The
   hook returns a payment reference or declines.
3. **Retries** the call with the payment reference injected.
4. **Records** the settled invocation to the WAL and emits a spend receipt
   binding the five hashes above, flushes the settled amount into the per-server
   cost meter (so `bernstein cost` rollups include it, tagged with the server
   name), and mirrors an `x402.settlement` event into the audit chain.

Replay mode serves the recorded settled response **without invoking the hook**,
so a replay can never double-settle.

## The settlement hook

A hook is any object with a `settle` method (or a plain callable wrapped in
`CallableSettlementHook`, or an operator command wrapped in
`CommandSettlementHook`). `amount_usd` is the amount Bernstein authorized from
the challenge and the mandate - never a value the hook chooses.

Example no-op hook (records intent, never actually pays - useful for a dry run):

```python
from bernstein.core.protocols.payments.x402 import (
    CallableSettlementHook,
    X402Config,
    X402Challenge,
)


def no_op_settle(challenge: X402Challenge, server: str, tool: str, amount_usd: float) -> str | None:
    # A real hook would call your payment rail here and return its reference.
    # This no-op declines every challenge, so nothing is ever settled.
    return None


config = X402Config(enabled=True, hook=CallableSettlementHook(no_op_settle))
```

To actually settle, return an opaque payment reference string instead of
`None`. Because the no-op declines, the original 402 surfaces unchanged - a safe
way to wire the path and watch refusal receipts accumulate before enabling real
payment.

An operator command hook reads a JSON request on stdin and prints
`{"payment_ref": "..."}` (or a null / non-zero exit to decline):

```python
from bernstein.core.protocols.payments.x402 import CommandSettlementHook, X402Config

config = X402Config(enabled=True, hook=CommandSettlementHook(argv=("./settle-x402.sh",)))
```

Bernstein only reads the opaque `payment_ref` from the hook - never a credential.

## Amounts

x402 challenges carry `maxAmountRequired` in atomic units of an arbitrary asset.
Converting that to USD needs the asset's decimals and a rate Bernstein does not
invent. A challenge that includes an explicit `amountUsd` is used directly;
otherwise supply a `price_resolver` on `X402Config`. When no USD figure can be
determined the settlement refuses honestly rather than guessing.

## CLI

```
bernstein gateway settlements                              # list recorded spend receipts
bernstein mandate verify-settlement <receipt_hash> --intent i.json
```

`verify-settlement` recomputes the receipt against its spine anchor, the settled
WAL invocation record, and the authorising consent receipt. Exit codes: `0`
verified, `1` no receipt / bad input, `2` mismatch (a mutated amount, challenge
digest, or invocation digest).

## On-disk layout

| Path | Contents |
|---|---|
| `.sdd/x402/settlements/<receipt_hash>.json` | The spend receipt binding + its anchor. |
| `.sdd/x402/refusals/<receipt_hash>.json` | Fail-closed refusal receipts. |
| `.sdd/lineage/x402-settlements/spine.jsonl` | The lineage spine anchoring every settlement / refusal. |
| `.sdd/mandates/receipts/<mandate_hash>.json` | The consent receipt each settlement was authorized under. |
| `.sdd/audit/` | The HMAC audit chain carrying `x402.settlement` / `x402.settlement_refused` events. |
| `.sdd/cost/ledger.jsonl` | Settled amounts, tagged with the server name alongside metered spend. |
