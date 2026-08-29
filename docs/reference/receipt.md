# `bernstein receipt`

Create and offline-verify a **result receipt bundle**: the portable unit of
trust for a worker submission. A bundle seals the patch, every gate's
command/exit-code/log, the task reference, the sandbox selection, and the
worker's DSSE-signed attestation over all of it into one artifact. Volunteer
workers also bind donor-budget line items (tasks, wall clock, and tokens) into
the signed bundle, including authorized, used, reserved, and remaining values.
`bernstein receipt verify` checks that artifact with no network access at
all — nothing here calls out to any service.

```bash
bernstein receipt verify bundle.json                 # trust the embedded key (TOFU)
bernstein receipt verify bundle.json --pubkey k.pem   # pin the worker key
bernstein receipt create spec.json --signing-key worker.pem -o bundle.json
```

## `bernstein receipt create`

Builds and signs a bundle from a JSON spec.

```bash
bernstein receipt create SPEC_PATH --signing-key KEY_PATH -o OUTPUT_PATH [--manifest-repo DIR]
```

| Flag | Required | Meaning |
|---|---|---|
| `--signing-key FILE` | yes | Worker Ed25519 private key (PEM) that signs the bundle. The worker identity (`keyid`, embedded public key) is derived from this key, so it cannot disagree with the signature — there is no separate "worker id" field to spoof. |
| `-o`, `--output FILE` | yes | Where the signed bundle is written. |
| `--manifest-repo DIRECTORY` | no | A repository root whose `.bernstein/volunteer.json` supplies `manifest_sha256`, read fresh from the file rather than taken from the spec. With this flag the spec may omit `manifest_sha256` entirely; without it, the field is required in the spec. See [the manifest reference](volunteer-manifest.md) for what the file being pointed at means. |

The spec is a JSON object naming the task, the patch, the gates that ran, a
manifest hash, adapter/model identifiers, the sandbox profile and selection
receipt, a timestamp, and the chain link (`anchor` + `length`) into the
worker's own receipt sequence:

```json
{
  "task": {"repo": "acme/widgets", "commit_sha": "a1b2c3d4e5f6", "issue_number": 42},
  "patch": "diff --git a/src/widget.py b/src/widget.py\n+print('hello')\n",
  "gates": [{"command": "pytest -q", "exit_code": 0, "log": "12 passed in 0.42s\n"}],
  "manifest_sha256": "6c1cbcae9d4d3f5f5a3b0c17f6a4e0a58e51e6c8b6f9c9f2f2c1d4a4e6b7c8d9",
  "adapter_id": "adapter.default.v3",
  "model_id": "claude-x",
  "sandbox_profile": "restricted-net-off",
  "selection_receipt": "sel-1",
  "created_at": "2026-08-18T00:00:00Z",
  "chain": {"anchor": "genesis", "length": 1}
}
```

```console
$ bernstein receipt create spec.json --signing-key worker.pem -o bundle.json
✓ wrote signed bundle to bundle.json
  digest: ede008207f77b97be83a3e4ef0f238cc6a67263d29e672d4ce2fd110ab6ff0d7
```

`digest` is the value a *successor* bundle in the same worker's chain cites
as its `chain.anchor` — see [Chain continuity](#chain-continuity) below.

## `bernstein receipt verify`

```bash
bernstein receipt verify BUNDLE_PATH [--pubkey FILE] [--prev-digest TEXT] [--expected-manifest-digest TEXT] [--json]
```

| Flag | Meaning |
|---|---|
| `--pubkey FILE` | Pin the worker key: verify the signature against this Ed25519 public key (PEM) instead of the one embedded in the bundle. |
| `--prev-digest TEXT` | Expected predecessor bundle digest — asserts chain continuity with a specific prior bundle. |
| `--expected-manifest-digest TEXT` | Expected [volunteer-manifest](volunteer-manifest.md) digest — ties this run to a policy the *project* declared, not one the worker chose. |
| `--json` | Emit the machine-readable form instead of the text summary. |

Verification is entirely offline. It checks, in order, collecting every
field-level failure rather than stopping at the first one:

1. the DSSE signature verifies against the resolved public key, and the
   predicate type is the result-receipt type;
2. the embedded bundle re-serialises to the digest the envelope attests
   (internal consistency);
3. the signing keyid matches the worker keyid the bundle names;
4. the patch hashes to its attested `patch_sha256`;
5. every gate log hashes to its attested `log_sha256`;
6. the chain link is well-formed, and — only when `--prev-digest` is
   given — matches it;
7. — only when `--expected-manifest-digest` is given — the manifest digest
   matches it.

Steps 6 and 7 are conditional on purpose, and that is the two independent
facts the next section is about.

## Two facts a `✓` does not, by itself, tell you

Verification reports the manifest digest and chain continuity as **checked**
or **not asked**, computed from two different inputs, and they can disagree
independently of each other and of the overall `ok` result:

- **`manifest_digest_checked`** — whether `manifest_sha256` was compared
  against `--expected-manifest-digest`. The bundle always *carries* a
  manifest digest; carrying one is not the same as it having been checked
  against anything, because the worker chose that value. This comparison is
  unconditional whenever `--expected-manifest-digest` is passed — reaching
  the end of verification without an error means it ran.
- **`prev_digest_checked`** — whether `chain.anchor` was compared against
  `--prev-digest`. Unlike the manifest check, this one can be *asked for and
  still not run*: it is the last arm of a chain-shape check, so a bundle
  whose `chain` object is malformed short-circuits before the anchor is ever
  looked at, even though `--prev-digest` was supplied.

Never read these as one "something was checked" question — they answer
different questions from different inputs, and a caller walking a chain of
bundles needs the answer to each independently, not their conjunction.

## What the three verdict shapes actually look like

### Verified

Trusting the bundle's own embedded key (trust-on-first-use — TOFU):

```console
$ bernstein receipt verify bundle.json
✓ bundle verifies against embedded key (trust on first use)
  keyid:  324be2dea8bc44461b0233e51fa48902ed6b1cc671e7739af2551e0bfe68f54e
  digest: ede008207f77b97be83a3e4ef0f238cc6a67263d29e672d4ce2fd110ab6ff0d7
  manifest: carried, NOT checked
  chain: carried, NOT checked
note: provenance requires pinning the worker key with --pubkey
note: the run is not tied to a declared policy without --expected-manifest-digest
```

TOFU authenticates the bytes against a key the worker itself supplied — it
does not establish **provenance** (that this key belongs to a specific,
known worker). Pinning the key with `--pubkey`, and naming a manifest with
`--expected-manifest-digest`, is what turns "internally consistent" into
"answers the two questions that matter":

```console
$ bernstein receipt verify bundle.json \
    --pubkey worker.pub.pem \
    --prev-digest genesis \
    --expected-manifest-digest 6c1cbcae9d4d3f5f5a3b0c17f6a4e0a58e51e6c8b6f9c9f2f2c1d4a4e6b7c8d9
✓ bundle verifies against pinned key
  keyid:  324be2dea8bc44461b0233e51fa48902ed6b1cc671e7739af2551e0bfe68f54e
  digest: ede008207f77b97be83a3e4ef0f238cc6a67263d29e672d4ce2fd110ab6ff0d7
  manifest: checked against 6c1cbcae9d4d3f5f5a3b0c17f6a4e0a58e51e6c8b6f9c9f2f2c1d4a4e6b7c8d9
  chain: checked against genesis
```

No `note:` lines this time — both open questions from the TOFU run were
answered.

### Refused

A bundle whose signed `patch` text and its attested `patch_sha256` have
drifted apart — the shape a build step that edits the raw bundle after
signing (rather than through the code that keeps the two in sync) would
produce:

```console
$ bernstein receipt verify tampered.json --pubkey worker.pub.pem
✗ bundle verification failed:
    patch: patch hashes to 1b216b8c72c81c8cbe9e207394c1ee4f2ff17cd9587564b33570ce85469ae9fd, bundle attests 346014ed57772ebeab7227c9dae7fe6f30715893402f97e7731f5954e604d9d9
```

Every field-level error names the exact field and both values, never just
"verification failed" — the same is true for a bad signature, a corrupted
gate log, a broken chain link, or a manifest digest mismatch. Exit code is
`1`.

### Malformed input

Input that is not a readable bundle at all — invalid JSON, valid JSON
missing the DSSE envelope fields, or an envelope missing `signatures` — is
refused before any verification is attempted:

```console
$ bernstein receipt verify not-a-bundle.json
✗ could not parse bundle: envelope at not-a-bundle.json is not valid UTF-8 JSON: Expecting value: line 1 column 1 (char 0)
```

Exit code is `1`, the same as a refusal.

**This is a third verdict, not a variety of "refused", and the distinction is
deliberate.** A parse failure says *this file is not a bundle*; a refusal
says *this is a bundle and it does not verify*. Only the second is a
statement about the run. A caller that collapses them will report an
operator who passed the wrong path exactly as it reports a tampered
artefact.

The code keeps the two apart rather than leaving the distinction to prose.
`load_bundle` raises `EnvelopeFormatError` for malformed input, and
`verify_cmd` catches that one class alongside `(OSError, ValueError,
json.JSONDecodeError)`. It deliberately does **not** catch the
`DSSEError` base: `EnvelopeSignatureError` and `EnvelopeTypeMismatchError`
inherit from it too, and catching the base would report a signature refusal
as malformed input — the exact collapse the paragraph above warns against.
`test_a_signature_refusal_is_not_reported_as_a_parse_failure` pins that,
so narrowing the catch to the base class later fails a test rather than
silently merging two verdicts.

## What a receipt does *not* attest

Everything above establishes that the bundle was not altered after signing
and that the worker's key signed it. None of it says **which rules the run
obeyed** — the worker chose every field, including `manifest_sha256`, unless
the caller supplies `--expected-manifest-digest` and it is compared and
matches. A receipt that verifies cleanly with no pinned key and no expected
manifest digest proves only that *some* worker produced *some*
self-consistent bundle; it does not prove which worker, and it does not
prove the run was measured against the project's actual policy.

Concretely, a clean `✓` with no `--pubkey` and no `--expected-manifest-digest`
does **not** mean:

- this bundle came from a worker you have any prior relationship with;
- the gates that ran are the gates the project declared in its
  [volunteer manifest](volunteer-manifest.md) — only that the gates listed
  *inside the bundle* produced the logs the bundle attests;
- the patch was reviewed, or is safe, or does anything sensible — only that
  the bytes reaching you are the bytes the worker signed.

## Chain continuity

`chain.anchor` in a bundle's spec names the digest of the *previous* bundle
in that worker's sequence (`"genesis"` for the first one); `chain.length` is
its 1-based position. Passing `--prev-digest <that predecessor's digest>`
when verifying the next bundle in the sequence checks that the link actually
holds — a caller walking a worker's history offline, one bundle at a time,
gets `chain: checked against ...` on every step rather than having to trust
an unlinked sequence of independently-valid bundles.

## Source

`src/bernstein/cli/commands/receipt_cmd.py`,
`src/bernstein/core/security/result_receipt_bundle.py`.
