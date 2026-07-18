# Browser activities: forensically replayable site checks and UI flows

The deterministic scheduler dispatches any agent modality behind one typed
activity boundary. The browser modality runs **site checks and UI flows** as
first-class activities: an operator schedules "check the deployed page after this
merge" or "walk the signup flow" with the same scheduler, budgets, and audit
guarantees a coding task gets.

A completed browser run is forensically replayable. Every observation the worker
saw and every action it took is content-addressed and folded into a chain, so a
verifier can reconstruct exactly what the browser saw and did, offline, months
later, from the chain alone.

A screenshot folder with a pass/fail note beside it cannot offer this. Nothing
binds the verdict to the pixels, nothing binds the pixels to the action that
produced them, and a swapped file is undetectable. A browser flow report here is
not prose with attachments; it is the anchored action journal itself.

## The anchor model

Around every action the worker captures the exact screenshot and DOM bytes it saw
**before** acting, stores both content-addressed in the run content store, and
folds them into that step's anchor:

```
dom_digest       = sha256(normalise(dom_bytes))
observation_hash = sha256(screenshot_bytes + dom_digest)
anchor           = sha256(canonical(prev_anchor, observation_hash, action))
```

Each step's record is the action receipt:

| Field | Meaning |
| --- | --- |
| `index` | Zero-based position in the flow |
| `action_kind` / `action_target` | The action verb and its target |
| `action_value_digest` | SHA-256 digest of any typed value, never the value |
| `screenshot_content_hash` | `sha256:` hash of the pre-action screenshot bytes |
| `dom_content_hash` | `sha256:` hash of the pre-action DOM bytes |
| `dom_digest` | Digest of the normalised DOM |
| `observation_hash` | Binds the anchor to the exact observed bytes |
| `prev_anchor` / `anchor` | The chain link and this step's identity |

Because each anchor folds in its predecessor, the step sequence is a single-parent
Merkle chain whose head is the run identity. A single altered observation changes
that step's anchor and every anchor after it, so divergence surfaces at an exact
index rather than as a flaky assertion.

DOM normalisation collapses runs of ASCII whitespace. Hashing raw markup would
change the digest on every reflow or minifier tweak and turn each replay into a
false divergence; the normalised form is still fully determined by the observed
bytes.

The report itself is content-addressed: its canonical JSON bytes are stored in the
content store, and that hash is anchored as the `artifact_hash` of the
`ActivityResult` dispatched with `ActivityKind.BROWSER`. The crossing is mirrored
into the HMAC-chained audit log, which records only hashes, never the observed
page.

## The verdict is recomputed, never trusted

A check record carries what the worker concluded, but verification does not take
that on faith. It reattaches the DOM bytes by hash and **re-evaluates** the
assertion. A report whose hashes all recompute but whose recorded verdict
disagrees with the anchored bytes fails, naming the check id.

The assertion vocabulary is deliberately closed and offline-evaluable, because
anything needing a live page could not be replayed:

| Kind | Holds when |
| --- | --- |
| `dom_contains` | The operand occurs in the normalised DOM |
| `dom_not_contains` | The operand does not occur in the normalised DOM |
| `screenshot_hash_equals` | The step's screenshot hash equals the operand |

## Running a flow

```python
from bernstein.core.agents.computer_use import Action, ActionKind, digest_typed_value
from bernstein.core.orchestration.activity import dispatch_activity
from bernstein.core.orchestration.browser_check import CheckKind
from bernstein.core.orchestration.browser_worker import (
    BrowserBudget,
    BrowserWorker,
    CheckSpec,
    FlowStep,
)

worker = BrowserWorker(
    store=store,
    budget=BrowserBudget(max_steps=32, max_observation_bytes=64 * 1024 * 1024),
    profile_root=sdd_dir / "browser-profiles",
)

run = worker.run(
    flow_id="checkout-smoke",
    start_url="https://shop.example/",
    steps=(
        FlowStep(
            action=Action(kind=ActionKind.NAVIGATE, target="https://shop.example/login"),
            checks=(CheckSpec(check_id="landing-ok", kind=CheckKind.DOM_CONTAINS, operand="Sign in"),),
        ),
        FlowStep(
            action=Action(
                kind=ActionKind.TYPE,
                target="#password",
                # Only the digest travels; the raw value never enters the chain.
                value_digest=digest_typed_value(secret),
            ),
        ),
    ),
    driver_factory=lambda profile_dir: browser_use_driver(profile_dir=profile_dir),
    final_checks=(CheckSpec(check_id="logged-in", kind=CheckKind.DOM_CONTAINS, operand="Welcome back"),),
)
dispatch_activity(run.result, stage_id="browser-0", journal=journal, chain=chain)
```

A check attached to a `FlowStep` is evaluated against the observation captured
before that step's action; `final_checks` are evaluated against the terminal
post-action capture the worker appends after the last action. So a flow with N
declared actions anchors N+1 steps.

Everything the worker writes is a pure function of the flow definition and the
observed bytes. No wall clock, no counter, and no network ordering enters any
hash, so two operators running the same flow over the same observations assemble
the byte-identical report and anchor the same `artifact_hash`.

## The driver interface

The worker never imports a concrete browser tool. It drives a `BrowserDriver`:
`navigate`, `act`, `screenshot`, `dom_snapshot`, `current_url`, `close`. A driver
maps its own vocabulary onto `ActionKind` before handing an action in, so a second
driver is added without touching the activity boundary.

Two drivers ship:

- `browser_use_driver(profile_dir=...)` builds the live driver backed by the
  optional `browser-use` package. It is an optional extra, so a missing install is
  a typed `BrowserDriverUnavailable` naming the install target
  (`pip install 'bernstein[browser]'`), never an `ImportError` surfacing from
  inside a run.
- `RecordedBrowserDriver(frames)` drives a fixed tape of recorded observations.
  This is the offline replay driver, and it is what makes replay determinism a
  checkable property: re-running a flow over the same tape must reproduce a
  byte-identical action sequence, report, and verdict.

## Isolation

Each run gets a profile directory named `sha256(flow_id)[:16]` under the worker's
profile root. Two concurrent browser tasks hold disjoint directories by
construction, with no allocator and no shared counter, so one task's cookie jar or
local storage cannot land inside another's. The profile is torn down on every exit
path, including driver failure, so no session survives into the next task.

## Cost caps and typed terminal states

`BrowserBudget` bounds the anchored steps and the cumulative observation bytes a
run may store. Per-step screenshots are large, so the byte cap is the one that
actually bounds a runaway flow; both are checked before the work happens and raise
`BrowserBudgetExceeded`.

Driver faults never surface as free text. They are normalised onto the closed
terminal-state set:

| Driver failure | Terminal state | Reason code |
| --- | --- | --- |
| `BrowserStepTimeout` | `TIMED_OUT` | `driver_timeout` |
| `BrowserDriverUnavailable` | `REFUSED` | `driver_unavailable` |
| `BrowserDriverError` | `FAILED` | `driver_error` |

A run that completes with a failing site check is still `COMPLETED`, with reason
code `checks_failed`: the failing check is a recorded verdict bound to anchored
bytes, not an exception.

A partial flow is still anchored. When a driver fails mid-flow the worker anchors
the state the flow died in as a pure capture, because the steps that did run are
exactly the evidence a post-incident reader needs, and the partial chain verifies
like any other.

## CLI

Submit a flow next to coding tasks:

```console
$ bernstein activity browser run --flow checkout.json --run run-42 --stage browser-0

Browser activity flow=checkout-smoke run=run-42 stage=browser-0
  terminal=completed reason=ok
  steps anchored: 3, head anchor: 4f1c...
  PASS landing-ok (dom_contains) at step 0
  PASS logged-in (dom_contains) at step 2
completed -- every check passed against anchored evidence.
```

Exit codes: `0` completed with every check passing, `2` completed with a failing
check, `3` refused, failed, or timed out.

Pass `--recording tape.json` to drive a recorded observation tape instead of a
live browser. That is how a completed run is re-executed offline to prove the
action sequence and verdict reproduce byte-for-byte.

A flow document is JSON:

```json
{
  "flow_id": "checkout-smoke",
  "start_url": "https://shop.example/",
  "steps": [
    {
      "action": {"kind": "navigate", "target": "https://shop.example/cart"},
      "checks": [{"id": "landing-ok", "kind": "dom_contains", "operand": "Sign in"}]
    },
    {"action": {"kind": "click", "target": "#checkout"}}
  ],
  "final_checks": [{"id": "order-placed", "kind": "dom_contains", "operand": "Order confirmed"}],
  "budget": {"max_steps": 16}
}
```

## Offline verification

`bernstein activity verify <run>` covers browser stages end to end, and
`bernstein activity browser verify <run>` shows the per-step and per-check detail:

```console
$ bernstein activity browser verify run-42
Browser activity verify run=run-42
  OK browser-0
    step OK index=0
    step OK index=1
    step OK index=2
    check OK landing-ok (recomputed passed=True)
    check OK logged-in (recomputed passed=True)
```

Alter one byte of one stored screenshot and verification fails at the exact step:

```console
$ bernstein activity browser verify run-42
Browser activity verify run=run-42
  MISMATCH browser-0 -- step 1: screenshot content hash mismatch (pinned 'sha256:...', recomputed 'sha256:...')
    step OK index=0
    step FAIL index=1 -- step 1: screenshot content hash mismatch (...)
```

Exit codes: `0` verified, `1` no run / no browser activity, `2` mismatch (tamper).
The check touches only the content store, so it holds with the network disabled,
and its output is a pure function of the report and the stored bytes: two verify
runs over the same completed run produce byte-identical verdicts.

`replay_reattach` returns the byte-identical snapshots for a completed run in a
fresh checkout, given the store.

## Why this shape

The report is not "a UI check plus an audit log". Strip the content store and the
report stops meaning anything, not merely stops logging: an anchor is an
unresolvable hash without the bytes behind it, and no check verdict can be
recomputed. The artifact the operator reads is itself the verifiable receipt of
the exact bytes the worker saw at each decision, and of the action it chose next.
