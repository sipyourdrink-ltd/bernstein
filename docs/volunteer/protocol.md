# Volunteer protocol documents

The volunteer cycle exchanges five kinds of message: a project advertises what
it offers, a donor advertises what they offer, a worker claims a task, a
worker submits a result, and a verifier records the outcome. Each is a small,
versioned, signed JSON document, defined once in
`src/bernstein/core/protocols/volunteer/` and independent of how it travels.

GitHub is one transport for these documents today (the manifest file, an
issue comment, a PR body). A self-hosted hub is another (plain JSON over
HTTP). Both read and write the *same* document and the *same* hash — the
transport is a rendering choice, not part of the document's identity.

## Shared substrate

Every document in this layer is wrapped in the same DSSE / in-toto v1
envelope, built by `documents.py`:

- **Canonical bytes** — `canonical_bytes()` serialises a document's dict as
  compact, recursively key-sorted, UTF-8 JSON. Two documents with the same
  field values always produce the same bytes, regardless of construction
  order.
- **Digest** — `canonical_hash()` is the SHA-256 hex digest of those bytes.
  This is a document's stable identity: unchanged by re-serialisation,
  changed by any field.
- **Sign / verify** — `sign_document()` / `verify_document()` wrap a
  canonical dict in an `Envelope` (reusing
  `bernstein.core.security.audit_dsse` wholesale — no reimplemented DSSE
  types) and check it back. Ed25519 is deterministic (RFC 8032 §5.1.6), so
  the same document plus the same key always produces the same signature.
- **Predicate type** — every document shares one base predicate type,
  `https://bernstein.run/attestations/volunteer/v1`; each document kind below
  also defines its own more specific predicate type (for example
  `.../claim/v1`) so a verifier can filter by kind without parsing the full
  payload first.

Each document module (`claim.py`, `project_card.py`, `worker_card.py`,
`submission.py`, `verdict.py`) builds its own envelope with its own predicate
type and its own `document_kind` string, using the same DSSE primitives
`documents.py` exposes rather than a fifth copy of the canonicalisation
logic.

### Schema version policy

`VOLUNTEER_DOCUMENT_SCHEMA_VERSION` (currently `"1.0.0"`) and each document's
own `*_SCHEMA_VERSION` constant are bumped only when a predicate or document
field set changes in a backward-incompatible way. An additive field that an
older verifier can carry-and-ignore does not require a bump — the field
already lives inside the predicate body and participates in the canonical
hash, so it is detectable without a version change.

## The five documents

### Project card

*What a project offers.* Built from a project's committed
`.bernstein/volunteer.json` (see
[the manifest reference](../reference/volunteer-manifest.md)) via
`ProjectCard.from_manifest(manifest, *, task_types, demand, submitted_at)`.

The dependency runs one way — `project_card.py` imports
`bernstein.core.volunteer.manifest`, never the reverse — so the manifest
loader gained no new dependency to make this projection possible.

| Field | Type | Notes |
| --- | --- | --- |
| `schema_version` | `str` | Defaults to `PROJECT_CARD_SCHEMA_VERSION`. |
| `demand` | `str` | Human-readable current demand (`"high"`, `"low"`, ...). Caller-supplied — the manifest has no demand field. |
| `task_types` | `list[str]` | Task type identifiers this project currently offers. Caller-supplied. |
| `requirements` | `str` | Human-readable summary derived from the manifest (license, sandbox tier, wall-clock ceiling, local-model eligibility). |
| `demand_snapshot` | `dict` | Structured snapshot: `manifest_digest` (the manifest's own digest, so a verifier can confirm which policy the card was built from), `gates_count`, `allowed_paths_count`, `task_label`. |
| `status` | `str` | `"active"` or `"paused"` — copied from the manifest's own `status`. |
| `submitted_at` | `str` | ISO-8601 timestamp, timezone-aware. |
| `notes` | `str \| None` | Optional. |

The manifest's `gates` are acceptance commands, not an advertisement — only
their **count** crosses into `demand_snapshot`. A gate's argv (for example
the exact pytest invocation) never appears on a project card.

Every string field is checked against a credential-name denylist
(case-insensitive `key`, `token`, `secret`, `password`, `credential`
substrings) before construction succeeds.

### Worker card

*What a donor offers.* A closed schema: every field has a fixed, explicit
type, and none is `dict[str, Any]` or otherwise open-ended. Unlike every
other document in this layer, a worker card cannot carry an unknown-field
extension bag — "preserve unknown fields verbatim" and "no free-form
secret-bearing field" are contradictory, and this document resolves that in
favour of being structurally incapable of carrying a credential.

| Field | Type | Notes |
| --- | --- | --- |
| `schema_version` | `str` | Defaults to `WORKER_CARD_SCHEMA_VERSION`. |
| `task_types` | `list[str]` | Must be drawn from a fixed vocabulary (`compute`, `data-processing`, `analysis`, `documentation`, `support`). |
| `capabilities` | `str` | Human-readable capability summary. |
| `cpu_ceiling`, `ram_ceiling`, `gpu_ceiling` | `str` | Each one of a fixed tier vocabulary (`micro`, `small`, `medium`, `large`, `xlarge`). |
| `sandbox_tier` | `str` | One of `microvm`, `container`, `baremetal`. |
| `availability_window` | `str` | Human-readable (`"weekdays 9am-5pm"`, `"full-time"`, ...). |
| `budget_posture` | `str` | One of `generous`, `modest`, `tight`, `free`. |
| `submitted_at` | `str` | ISO-8601 timestamp, timezone-aware. |
| `notes` | `str \| None` | Optional. |

Every string field value is rejected if it contains a credential-like
substring (`key`, `token`, `secret`, `password`, `credential`,
case-insensitive) — a second line of defense on top of the closed field set,
since a legitimately-typed string field could otherwise smuggle a value that
merely looks like a capability description.

A worker card is never emitted through the GitHub projection: the GitHub
comment renderer (`github.py`) only knows claim, submission, verdict, and
merge-receipt documents. Worker cards are published only where a donor
explicitly enrolls with a hub — the forge (GitHub-native) path never
publishes one, so a donor's availability and capacity stay private by
default.

### Claim

*Worker X takes task Y at time T.* The simplest document in the layer, and
the one the shared substrate and conformance harness were built and proved
against first.

| Field | Type | Notes |
| --- | --- | --- |
| `schema_version` | `str` | Defaults to `CLAIM_SCHEMA_VERSION`. |
| `worker_id` | `str` | Opaque identifier for the claiming worker. Non-empty. |
| `task_id` | `str` | Opaque identifier for the task. Non-empty. |
| `claimed_at` | `str` | ISO-8601 timestamp, timezone-aware. |

`protocols/volunteer/claim.py` is distinct from
`bernstein.core.volunteer.claim`, which implements the best-effort
GitHub-comment etiquette for coordinator-free claim signalling. The two
serve different roles: this module is cryptographically verifiable evidence,
the other is a human-observable signal.

### Submission

*A patch reference plus a receipt bundle reference.* A submission is
deliberately thin: everything else — the patch, the gate logs, the
manifest digest the gates ran against — already lives inside the signed
[result-receipt bundle](../../src/bernstein/core/security/result_receipt_bundle.py)
it points at. A submission does not recompute or re-attest anything the
bundle already carries; it only routes a verifier to it.

| Field | Type | Notes |
| --- | --- | --- |
| `schema_version` | `str` | Defaults to `SUBMISSION_SCHEMA_VERSION`. |
| `receipt_bundle_digest` | `str` | The bundle's own `.digest` — a 64-char sha256 hex string, carried verbatim, never recomputed here. |
| `receipt_bundle_location` | `str` | Where a verifier fetches the bundle: a URL, a PR artifact reference, or a hub path. |
| `task_ref` | `dict` | `repo`, `commit_sha`, `issue_number` — field-for-field the same shape as the receipt bundle's own `TaskRef`, duplicated on purpose so a verifier can route without fetching the bundle first. The two copies are allowed to disagree; that disagreement is itself a signal worth surfacing, not something to prevent by only keeping one copy. |
| `submitted_at` | `str` | ISO-8601 timestamp, timezone-aware. Optional (defaults to unset). |

### Verification verdict

*The gate re-run outcome.* A maintainer-facing pass/fail summary, not a
second copy of the bundle's full logs — those stay in the receipt bundle,
which already tamper-protects them field-by-field.

| Field | Type | Notes |
| --- | --- | --- |
| `schema_version` | `str` | Defaults to `VERDICT_SCHEMA_VERSION`. |
| `submission_digest` | `str` | Which submission this verdict is about — chains verdict → submission → bundle, rather than pointing at a task id directly (which would let a verdict be replayed against a different submission for the same task). |
| `gate_results` | `list[dict]` | One `{"command": str, "passed": bool}` entry per gate. No log text. |
| `verifier_keyid` | `str` | keyid of the verifier who signed this verdict. |
| `recommendation` | `str` | One of `accept`, `request-changes`, `reject`, `no-gates` — a closed enum, not free text, so a maintainer-facing UI can filter and sort by outcome. |
| `verified_at` | `str` | ISO-8601 timestamp, timezone-aware. |
| `notes` | `str \| None` | Optional. |

## Two conformance projections

`conformance.py` proves the property that makes GitHub one transport among
several rather than a structural dependency:

```
canonical_hash(original_dict)
    == canonical_hash(from_github_projection(to_github_projection(original_dict)))
    == canonical_hash(from_hub_projection(to_hub_projection(original_dict)))
```

- **Hub projection** — the canonical dict serialised as plain JSON. The
  simplest possible projection; what a hub or webhook payload carries.
- **GitHub projection** — the document embedded in a fenced JSON block
  inside an issue comment or PR body, wrapped in a `<!-- bernstein-volunteer-doc -->`
  marker so it can be reliably stripped on read-back. Readable by a human
  looking at the comment, parseable by a machine reading the same string.

Both projections are lossless. `ConformanceHarness` is generic — a document
type plugs in by registering a `to_canonical_dict` / `from_canonical_dict`
pair, not by subclassing. `github.py` builds the per-document-kind GitHub
projection helpers (`to_github_claim`, `to_github_submission`,
`to_github_verdict`, plus a universal `to_github_projection` /
`from_github_projection` dispatcher) on top of the same harness for claim,
submission, verdict, and merge-receipt documents. Project cards and worker
cards are not part of the GitHub-comment surface: a project card already has
a native GitHub-native transport (the manifest file itself), and a worker
card is deliberately never rendered through the forge path at all (see
above).

## Related: merge receipt

Beyond the five documents above, `receipt.py` defines a `MergeReceipt` — the
record that a submission was merged and any reward decision made. It closes
the cycle (claim → submission → verdict → merge receipt) using the same DSSE
substrate and predicate-type discipline as every other document here.

## Verifying a document offline

Any party holding the signer's Ed25519 public key can verify a document
without importing bernstein code: parse the DSSE envelope, check the
signature over the PAE-encoded payload, confirm the predicate type matches
the document kind, and recompute the embedded document's canonical hash
against the envelope's attested subject digest. Each module's
`build_*_envelope` / `verify_*_envelope` pair (`build_claim_envelope` /
`verify_claim_envelope`, and the equivalent pair for every other document
kind) is the reference implementation of that check.
