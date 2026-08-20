# Adapter conformance canary

Upstream agent CLIs release on their own schedule, and a release can
break an adapter contract (rename a flag, drop a subcommand, change
output shape) without any change on our side. Version-floor advisories
(`bernstein doctor`, see `src/bernstein/adapters/advisories.py`) warn
about known-old versions, but they cannot catch a *fresh* upstream
release breaking a contract. The canary closes that gap: adapter
breakage becomes our finding, not a user's broken unattended run.

## What runs

A nightly workflow
(`.github/workflows/adapter-conformance-canary.yml`) drives
`scripts/adapter_canary.py`, which runs the canary matrix defined in
`src/bernstein/adapters/canary.py`:

* every **primary adapter** (`agy`, `aider`, `claude`, `codex`,
  `copilot`, `droid`, `gemini`, `kimi`, `opencode`, `pydantic_ai`,
  `qwen`) against whatever upstream version the runner installed that
  night;
* one **pinned tiny goal** on the cheapest usable model per adapter, so
  daily spend stays bounded and run-to-run diffs isolate upstream drift;
* the same **in-process conformance check** operators get from
  `bernstein adapters check`: the installed binary's `--help` surface is
  matched against the adapter's declared contract
  (`tests/contract/contracts/<adapter>.yaml`).

## Every probe is a receipt

Each probe seals a content-addressed receipt: canonical JSON whose
SHA-256 is its identity. Two runs that observed the same upstream
surface at the same timestamp produce byte-identical receipts; a
mutated receipt fails verification
(`bernstein.adapters.canary.verify_canary_receipt`) exactly like a
tampered chain entry. Receipt hashes are mirrored into the HMAC audit
chain (`adapter.canary_receipt` events) by the nightly entrypoint
(`scripts/adapter_canary.py`), so a canary finding is reconstructable
offline rather than living only in a CI log. The chain segment is
written under the run's `receipts/audit-chain/` directory and uploaded
with the receipts artifact, so the receipt-to-chain binding survives the
ephemeral runner: recompute a receipt file's SHA-256 and match it
against the `receipt_sha256` of the persisted `adapter.canary_receipt`
entry.

## What `last_green.json` rows must look like

`load_last_green` validates each row at the JSON boundary instead of
coercing it. A row that does not satisfy the shape it claims is **dropped
with a warning**, and the rest of the table still loads -- one bad row
must not empty the projection, because an empty projection makes every
`doctor` staleness check a silent no-op.

| Field | Accepted | Rejected |
|---|---|---|
| `binary` | non-empty string; surrounding whitespace is stripped | `null`, numbers, lists, objects, `""` |
| `version` | non-empty string; surrounding whitespace is stripped | `null`, numbers, lists, objects, `""` |
| `receipt_sha256` | exactly 64 lowercase hex characters | uppercase hex, truncated hashes, any non-hash string, non-strings |
| `recorded_at` | a timestamp `datetime.fromisoformat` accepts, including a trailing `Z` | `"yesterday"`, epoch integers, objects, non-strings |

A row missing any of these keys is dropped, as before.

**If you maintain a projection this repo did not generate**, check it
against the table above before upgrading: a hand-written or older file
whose `receipt_sha256` is not a full lowercase hash, or whose
`recorded_at` is not ISO 8601, will now be dropped rather than loaded.
The symptom is `CANARY_UNKNOWN` at admission and a warning-only `doctor`
result for that adapter, and the cause is named in a
`last-green row ... malformed` warning on the loader's logger. Rows the
canary itself writes are already in this shape; regenerate with
`uv run python scripts/adapter_canary.py --update-docs` if in doubt.

The reason for validating rather than coercing: `str(value)` renders
`None` as `"None"` and a list as its repr, so a corrupt row used to load
as a populated entry that admission and `doctor` then read as a
receipt-backed claim. The table is a projection of receipts and every row
is meant to be independently checkable against its receipt file; a value
that was never a hash cannot be checked against anything.

## Regression handling

* A regression must repeat: **two consecutive failures with the same
  failure fingerprint** are required before the canary proposes an
  issue, so one upstream flake never pages anyone.
* A `--help` that advertises **none** of a contract's required tokens (or
  prints nothing) is classified as an **inconclusive `skip`, not a
  `fail`** -- an installed CLI cannot legitimately drop its entire
  required surface in one release, so this signals a broken, paginated,
  or wholesale-redesigned `--help` (or a shim binary on `PATH`) that an
  operator must investigate, rather than genuine per-flag drift. A
  *partial* miss (at least one required token still present) remains a
  real drift `fail`. The `skip` is independent of the process exit code.
  The skip transcript records the **resolved binary path**, so a
  shadowed/wrong binary on `PATH` is distinguishable from real drift.
* Issues are **deduped on the failure fingerprint** (adapter + version +
  failure lines): the same regression never opens two issues, while a
  new upstream version failing fresh reports again.
* The opened issue carries the failing conformance transcript and the
  receipt hash, so the finding is reproducible from the issue alone.

## Chronic-skip handling

A `skip` is not a conformance break, but an adapter that skips for the
**same reason on three consecutive runs** is silently unverified -- the
blind spot the canary exists to close. Such a streak opens a
distinctly-labeled tracking issue (title: *"Adapter conformance canary
skip streak"*, never *"regression"*), so a degraded probe becomes visible
without being conflated with confirmed drift:

* The skip streak counts on its own counter and threshold
  (`SKIP_ISSUE_THRESHOLD`), independent of the failure path, and is
  deduped on an (adapter, skip-reason) fingerprint so it opens **one**
  issue per chronic reason.
* A `pass` resets the streak; a different skip reason restarts it.
* A chronic skip never reddens the job -- the workflow stays advisory;
  escalation-to-issue is the mechanism, not a red cron.

## Last-green table

The table below is regenerated by the canary from passing receipts; it
is a projection, never a hand-maintained list. A primary adapter with no
passing receipt has no last-green row, so the primary-adapter list above
and this table diverge until the next green canary run for that adapter.
Each row names the
receipt hash prefix that attested it. A row whose `recorded_at` is older
than seven days is annotated `(stale)`: the canary refreshes passing rows
nightly, so a frozen row is no longer evidence the surface still conforms.

`bernstein doctor` reads the same projection (shipped as
`src/bernstein/adapters/last_green.json`) and, for every locally installed
matrix adapter, warns when the installed version is *ahead* of last-green
(the canary has not verified that release yet), when the adapter has **no
last-green row at all** (installed but never certified), and when its
last-green row is **stale**. So an operator sees an absent or frozen
adapter locally, not only in this table.

### Adapters that structurally cannot earn a last-green row

Listed here only when the cause is permanent -- something no nightly run
can clear. An adapter that is merely uncertified today does not belong on
this list: that state is transient, the table above already reports it,
and a hand-maintained copy would drift the first night the adapter
passes. An inconclusive probe is the common transient cause and is
covered under *Chronic-skip handling*.

* **`agy`** (Antigravity) is closed-source with a **manual, no-CI install
  path** (`install.method: manual`, empty spec in
  `tests/contract/contracts/agy.yaml`): the CI runner cannot install it,
  so its last-green row is refreshed only by an operator running the canary
  locally and can freeze while peer rows refresh nightly. Its row is
  therefore expected to read `(stale)` on the CI-shipped projection; treat
  `agy` as an operator-verified local check, not an automation-fresh row.

<!-- last-green:begin -->
| Adapter | Binary | Last-green version | Verified | Receipt |
|---|---|---|---|---|
| agy | `agy` | 1.0.0 | 2026-07-11T05:57:23Z (stale) | `006fb946868d` |
| aider | `aider` | 0.86.2 | 2026-08-20T05:18:46Z | `5413248ed558` |
| claude | `claude` | 2.1.237 | 2026-08-20T05:18:46Z | `b45aadba96dd` |
| codex | `codex` | 0.148.0 | 2026-08-20T05:18:46Z | `9c1d90466631` |
| copilot | `copilot` | 1.0.80 | 2026-08-20T05:18:46Z | `8113ed343009` |
| gemini | `gemini` | 0.56.0 | 2026-08-20T05:18:46Z | `7c740174dd6d` |
| kimi | `kimi` | 1.49.0 | 2026-08-20T05:18:46Z | `12141a3188ec` |
| opencode | `opencode` | 1.18.18 | 2026-08-20T05:18:46Z | `655c8689ad99` |
| pydantic_ai | `clai` | 2.32.1 | 2026-08-20T05:18:46Z | `46b1101f8cc2` |
| qwen | `qwen` | 0.21.14 | 2026-08-20T05:18:46Z | `b09ba6b8c4eb` |
<!-- last-green:end -->

## Operator knobs

* Pin an adapter for unattended runs to its last-green version when
  doctor warns.
* Run one probe locally:
  `uv run python scripts/adapter_canary.py --adapter agy`.
* Regenerate this table from a local run:
  `uv run python scripts/adapter_canary.py --update-docs`.
