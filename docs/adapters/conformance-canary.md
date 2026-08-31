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

## The projection is re-derived, not trusted

`last_green.json` and the table on this page are both written by one run,
so checking them against each other proves they came from the same
generator rather than that the generator was right. A stale row carried
forward, a digest recorded for a receipt that was never produced, or an
adapter dropped between the receipt set and the JSON all reproduce
identically into both, and every downstream consistency check still
passes.

`verify_last_green_projection` closes that gap by re-reading the receipts
and asking whether they produce the committed rows. The nightly workflow
runs it as its own step (`--verify-projection`), in a fresh process,
after `Open threshold-crossing regression issues` and before PR proposal,
so a projection mismatch is caught while receipts are still on local disk
without suppressing threshold-crossing regression issue creation.

It checks both directions for the adapters actually in play: every
passing receipt must have a row carrying that receipt's digest and this
run's timestamp, and every row claiming this run's timestamp must have a
passing receipt behind it. A row that is simply older and untouched by
the run is out of scope, which is why `agy` sitting weeks behind the
others, and `droid` having no row at all, are not findings.

Running the check against a **downloaded** artifact bundle instead is
bounded by the receipts artifact's 30-day retention. The in-workflow step
has no such limit; it never touches the artifact store.

## The proposal carries only what the canary regenerates

The regeneration reaches `main` through a long-lived pull request on
`bot/adapter-canary-last-green`. That branch is rebuilt each night by
`scripts/canary_propose_branch.py` on `origin/main` **as fetched at commit
time**, not on the workflow checkout.

The distinction matters because the checkout is taken before the matrix
probes every adapter, and a night that regenerates nothing exits before
pushing, so the branch can sit at an older commit across nights. Anything
merged into `main` inside that window then appears in the proposal as a
*revert* of work the canary never touched, and a squash merge would land it.
That is #4496: `docs/security/receipt-format-spec.md` came back to a form
predating the edits #4489 had landed.

Rebuilding on the fetched base removes the drift; the script then asserts the
staged changed-file set against the merge base and fails if it is anything
other than `src/bernstein/adapters/last_green.json` and
`docs/adapters/conformance-canary.md`. A stray path stops the proposal rather
than being dropped with a warning - dropping it would let a genuine projection
bug leave the tree unnoticed, which is the fault this check exists to surface.

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
An adapter that probed `absent` on the regenerating run (its binary was
not on `PATH`, so the probe never ran) is annotated `(not probed)`
instead: the row is *unverified because the probe could not run*, not
*unverified because the adapter stopped passing* (#4387). The last known
good version and receipt stay visible -- the projection is for attested
facts -- but the annotation stops the row from reading as a quiet
regression.

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
  therefore expected to read `(not probed)` on the CI-shipped projection
  (the CI probe resolves no binary, so the run annotates it unverified
  rather than stale); treat `agy` as an operator-verified local check, not
  an automation-fresh row.

<!-- last-green:begin -->
| Adapter | Binary | Last-green version | Verified | Receipt |
|---|---|---|---|---|
| agy | `agy` | 1.0.0 | 2026-07-11T05:57:23Z (not probed) | `006fb946868d` |
| aider | `aider` | 0.86.2 | 2026-08-30T10:14:52Z | `21be1c27a63e` |
| claude | `claude` | 2.1.251 | 2026-08-30T10:14:52Z | `e998c00b1e08` |
| codex | `codex` | 0.151.0 | 2026-08-30T10:14:52Z | `538298a2b76d` |
| copilot | `copilot` | 1.0.82 | 2026-08-30T10:14:52Z | `31eb755450b1` |
| gemini | `gemini` | 0.57.0 | 2026-08-30T10:14:52Z | `db278cc19270` |
| kimi | `kimi` | 1.49.0 | 2026-08-30T10:14:52Z | `515fc853a0b6` |
| opencode | `opencode` | 1.18.25 | 2026-08-30T10:14:52Z | `aa3b51f200f0` |
| pydantic_ai | `clai` | 2.36.0 | 2026-08-30T10:14:52Z | `79712e98e1bf` |
| qwen | `qwen` | 0.22.3 | 2026-08-30T10:14:52Z | `42b1d0bf333a` |
<!-- last-green:end -->

## Operator knobs

* Pin an adapter for unattended runs to its last-green version when
  doctor warns.
* Run one probe locally:
  `uv run python scripts/adapter_canary.py --adapter agy`.
* Regenerate this table from a local run:
  `uv run python scripts/adapter_canary.py --update-docs`.
