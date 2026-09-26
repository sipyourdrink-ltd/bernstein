# Signed results from workers you do not control

A patch arrives from a machine you do not operate: a volunteer's laptop, a CI
runner in another account, a contractor's sandbox. It comes with a claim that
the tests passed. Before you merge it you want to know, offline:

- which worker key produced it;
- that the patch and the gate logs are exactly the ones that key signed;
- that the run was held to the policy your project declared, not one the
  worker picked;
- that it continues that worker's previous submission rather than replacing
  or skipping one.

A **result receipt bundle** carries all of that in one file, and
`bernstein receipt verify` checks it with no network access.

## How it works

The bundle is a DSSE envelope over an in-toto statement. The signed payload
holds the patch, every gate's command, exit code and log, the task reference,
the sandbox profile, adapter and model identifiers, a manifest digest and a
chain link (`anchor` + `length`) into the worker's own sequence of receipts.

`bernstein receipt verify` checks, and reports every failed field rather than
stopping at the first:

| Check | Always | Only when you pass |
|---|---|---|
| DSSE signature over the payload | yes | `--pubkey` pins the key; otherwise the embedded key is trusted on first use |
| Payload re-serialises to the attested digest | yes | |
| Signing keyid matches the worker named in the bundle | yes | |
| Patch and every gate log hash to their attested digests | yes | |
| Manifest digest equals the policy you expect | | `--expected-manifest-digest` |
| Chain anchor links to a specific predecessor | | `--prev-digest` |

The last two are reported as `checked` or `NOT checked` on every run, so a
green result never hides which questions were actually asked. Details:
[`bernstein receipt` reference](../reference/receipt.md).

## Worked example

Run against Bernstein 3.20.0 with an Ed25519 key made by `openssl`. The
bundles come from the
[receipt tutorial](../tutorials/signed-result-receipt.md), which shows how
they were created. Digests will differ on your machine.

### Trust on first use is not provenance

With no key pinned, verification passes and tells you what it did not check:

```console
$ bernstein receipt verify bundle.json
✓ bundle verifies against embedded key (trust on first use)
  keyid:  5aafdec2731ca7e9b0823690b17654f61e023df4f16c57518221b1cbc80936fc
  digest: 860255ee43404950287b2ca802e89c516cd378d6a9c739568911ca0cad8ab0ba
  manifest: carried, NOT checked
  chain: carried, NOT checked
  note: provenance requires pinning the worker key with --pubkey
  note: the run is not tied to a declared policy without --expected-manifest-digest
```

### Pin everything you know

Pin the worker's public key, the digest of your declared policy file, and the
digest of the worker's previous bundle:

```console
$ bernstein receipt verify bundle2.json --pubkey worker.pub.pem \
    --expected-manifest-digest 1b0d099c64fa735ec55e85e8fc6fcd6c4aba122da238803970c10955719385ce \
    --prev-digest 860255ee43404950287b2ca802e89c516cd378d6a9c739568911ca0cad8ab0ba
✓ bundle verifies against pinned key
  keyid:  5aafdec2731ca7e9b0823690b17654f61e023df4f16c57518221b1cbc80936fc
  digest: 3ec0160daded1ddd8c676fb3f45309926624c450c0e3ab988f9faae157a1e1c5
  manifest: checked against 1b0d099c64fa735ec55e85e8fc6fcd6c4aba122da238803970c10955719385ce
  chain: checked against 860255ee43404950287b2ca802e89c516cd378d6a9c739568911ca0cad8ab0ba
```

### Three ways a submission fails

The patch was changed after signing (`return a + b` edited to
`return a + b + 1` inside the signed payload):

```console
$ bernstein receipt verify edited.json --pubkey worker.pub.pem
✗ bundle verification failed:
    envelope: signature verify failed for keyid='5aafdec2731ca7e9b0823690b17654f61e023df4f16c57518221b1cbc80936fc':
$ echo $?
1
```

The bundle is genuine but was not signed by the key you expect:

```console
$ bernstein receipt verify bundle.json --pubkey other.pub.pem
✗ bundle verification failed:
    envelope: signature verify failed for keyid='5aafdec2731ca7e9b0823690b17654f61e023df4f16c57518221b1cbc80936fc':
$ echo $?
1
```

The bundle does not continue the submission you last accepted:

```console
$ bernstein receipt verify bundle2.json --pubkey worker.pub.pem \
    --prev-digest 0000000000000000000000000000000000000000000000000000000000000000
✗ bundle verification failed:
    chain.anchor: anchor 860255ee43404950287b2ca802e89c516cd378d6a9c739568911ca0cad8ab0ba does not link to predecessor 0000000000000000000000000000000000000000000000000000000000000000
$ echo $?
1
```

## Put it in a review gate

`--json` gives a machine-readable verdict with `ok`, `manifest_digest_checked`,
`prev_digest_checked` and a list of `errors`, and the exit code is non-zero on
any failure:

```bash
bernstein receipt verify submission.json --json \
  --pubkey "keys/${WORKER}.pub.pem" \
  --expected-manifest-digest "$(shasum -a 256 policy.yaml | cut -d' ' -f1)" \
  --prev-digest "$(cat "state/${WORKER}.last-digest")"
```

Store the accepted bundle's `digest` as the next `--prev-digest` for that
worker.

## What this does and does not prove

- It proves the patch and gate logs are byte-for-byte what the pinned key
  signed, under the policy digest and chain position you asked about.
- It does not re-run the gates. A worker that controls its own key can sign a
  log of tests it never ran; the receipt makes that claim attributable to the
  key, not true. Re-run the gates on your side before merging.
- The policy digest in this example is `shasum` of a local file. Projects
  using the volunteer flow declare it in `.bernstein/volunteer.json`; see the
  [volunteer manifest](../reference/volunteer-manifest.md) and
  `receipt create --manifest-repo`.

## Related

- Step by step: [Sign and verify a result receipt](../tutorials/signed-result-receipt.md)
- [`bernstein receipt` reference](../reference/receipt.md)
- [Verification evidence bundles](../operations/evidence-bundles.md)
