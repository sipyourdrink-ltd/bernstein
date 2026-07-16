# Adapter security floor

Bernstein spawns third-party CLI coding agents with full task context: the
binary it execs receives the task, the workspace, and the worker's credential
scope. The spawn boundary is therefore the most privileged hand-off in the
system. The adapter security floor promotes the curated minimum-safe-version
map from an advisory into an enforced spawn-time contract, and makes every
decision a verifiable receipt.

## What it does

- **Enforced at spawn.** Before a tracked adapter is spawned, its installed
  version is compared against the minimum-safe floor in
  `src/bernstein/adapters/advisories.py`. A below-floor binary is refused by
  default, so a known-unsafe upstream CLI no longer receives task context.
- **The refusal is the proof.** Each preflight decision (permit, refusal, or
  warn-only override) is sealed into a content-addressed receipt and mirrored
  into the HMAC audit chain as an `adapter.spawn_preflight_receipt` event. The
  refusal is not a log line about a version check; it is a tamper-evident
  artefact anchored to the chain.
- **Permits carry the verdict too.** A refusal-only record would prove nothing
  about the windows in between, so permits are recorded as well. Given a
  contiguous audit-chain slice, a verifier can prove offline that no
  below-floor adapter spawn was permitted during a window, and chain
  continuity makes a gap in the slice detectable rather than silently
  exculpatory.
- **Doctor posture is a receipt.** `bernstein doctor` seals the environment's
  version posture into an `adapter.version_posture` receipt, so "only
  floor-satisfying binaries were spawnable in this environment during window X"
  is provable offline, not merely a console print an operator may never run.
- **The canary refuses below-floor certification.** The nightly conformance
  canary declines to certify a below-floor version and records the refusal as
  a receipt; the last-green projection can never advance onto a below-floor
  version.

## Policy

Block-by-default. To permit a below-floor spawn (recording a warn-override
receipt rather than refusing), set:

```
export BERNSTEIN_ADAPTER_FLOOR_POLICY=warn
```

Any other value keeps block-by-default.

## Tamper detection

Every receipt pins the floor map's content hash. A floor map mutated after a
receipt was sealed yields a hash mismatch at verification, so tampering with
the map itself is caught, not only tampering with a receipt body.

## Refreshing the floor map

The floor map is data. A bump lands as a data-only diff in
`src/bernstein/adapters/advisories.py` (regenerated between the floor-map
markers), accompanied by a signed update receipt that binds the old and new
floor-map content hashes:

```
uv run python scripts/refresh_adapter_floors.py --feed feed.json \
    --write --receipt-out floor_update_receipt.json --anchor
```

The feed is machine-readable advisory data:

```json
{
  "schema_version": 1,
  "adapters": {
    "aider": {
      "min_safe_version": "0.60.0",
      "advisory_id": "BSA-0001",
      "note": "aider below 0.60.0 is known-unsafe for spawned use; upgrade recommended."
    }
  }
}
```

The update receipt is anchored to the chain as an `adapter.floor_update_receipt`
event, so a reviewer can prove offline which floor map was in force when a
spawn-preflight or version-posture receipt was recorded.
