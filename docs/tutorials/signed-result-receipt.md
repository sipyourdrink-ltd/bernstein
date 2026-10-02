# Sign and verify a result receipt

In this tutorial you play both sides of a submission. As the **worker** you
fix a bug, run the tests, and seal the patch and test log into a signed
receipt bundle. As the **maintainer** you verify that bundle offline, pin the
worker's key and your declared policy, check that a second bundle continues
the first, and watch three kinds of tampering fail.

**Time:** about 15 minutes. **You need:** Bernstein installed
([Install](../getting-started/install.md)), `git`, OpenSSL 3.x (for Ed25519
keys), `jq` 1.6 or later, and Python with `pytest`.

Output below is from Bernstein 3.20.0. Your keys, digests and commit ids will
differ; the shape of the output and the exit codes will not.

## 1. Create the worker's key

```bash
mkdir receipt-demo && cd receipt-demo
openssl genpkey -algorithm ed25519 -out worker.pem
openssl pkey -in worker.pem -pubout -out worker.pub.pem
```

`worker.pem` stays with the worker. `worker.pub.pem` is what the maintainer
pins.

!!! success "Check"
    `head -1 worker.pub.pem` prints `-----BEGIN PUBLIC KEY-----`.

## 2. Produce something to sign

A one-file project with a bug and a test for it:

```bash
mkdir calc && cd calc && git init -q
printf 'def add(a, b):\n    return a - b\n' > calc.py
printf 'from calc import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n' > test_calc.py
git add -A && git commit -q -m init
printf 'def add(a, b):\n    return a + b\n' > calc.py
cd ..
```

Capture the fix and run the gate:

```console
$ git -C calc diff > fix.patch
$ (cd calc && python -m pytest -q -p no:cacheprovider) > pytest.log
$ cat pytest.log
.                                                                                            [100%]
1 passed in 0.03s
```

The maintainer's declared policy is any file whose digest both sides agree
on. Here it is a small YAML file:

```console
$ printf 'min-reviewers: 1\nnetwork: off\n' > policy.yaml
$ shasum -a 256 policy.yaml
1b0d099c64fa735ec55e85e8fc6fcd6c4aba122da238803970c10955719385ce  policy.yaml
```

!!! success "Check"
    `pytest.log` ends in `1 passed` and `fix.patch` contains
    `+    return a + b`.

## 3. Write the receipt spec and sign it

The spec names the task, carries the patch and every gate's command, exit
code and log, and links into the worker's chain of receipts. Save this as
`mkspec.sh` so you can reuse it in step 5:

```bash
jq -n \
  --rawfile patch fix.patch \
  --rawfile log pytest.log \
  --arg commit "$(git -C calc rev-parse --short=12 HEAD)" \
  --arg manifest "$(shasum -a 256 policy.yaml | cut -d' ' -f1)" \
  --arg anchor "${ANCHOR:-genesis}" --argjson length "${LENGTH:-1}" \
  '{task: {repo: "example/calc", commit_sha: $commit, issue_number: 7},
    patch: $patch,
    gates: [{command: "pytest -q", exit_code: 0, log: $log}],
    manifest_sha256: $manifest,
    adapter_id: "adapter.default.v3", model_id: "example-model",
    sandbox_profile: "restricted-net-off", selection_receipt: "sel-1",
    created_at: "2026-09-24T18:30:00Z",
    chain: {anchor: $anchor, length: $length}}'
```

The first receipt in a worker's chain uses `anchor: "genesis"` and
`length: 1`. Build the spec and sign it:

```console
$ bash mkspec.sh > spec.json
$ jq '{task, manifest_sha256, chain}' spec.json
{
  "task": {
    "repo": "example/calc",
    "commit_sha": "6cffa4faf0bb",
    "issue_number": 7
  },
  "manifest_sha256": "1b0d099c64fa735ec55e85e8fc6fcd6c4aba122da238803970c10955719385ce",
  "chain": {
    "anchor": "genesis",
    "length": 1
  }
}

$ bernstein receipt create spec.json --signing-key worker.pem -o bundle.json
✓ wrote signed bundle to bundle.json
  digest: 860255ee43404950287b2ca802e89c516cd378d6a9c739568911ca0cad8ab0ba
```

!!! success "Check"
    `jq -r .payloadType bundle.json` prints `application/vnd.in-toto+json`:
    the bundle is a DSSE envelope around an in-toto statement. Keep the
    `digest`; the next receipt in the chain cites it.

## 4. Verify as the maintainer

Start with no key pinned. It passes, and says what it did not check:

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

Pin the worker key and your policy digest:

```console
$ bernstein receipt verify bundle.json --pubkey worker.pub.pem \
    --expected-manifest-digest 1b0d099c64fa735ec55e85e8fc6fcd6c4aba122da238803970c10955719385ce
✓ bundle verifies against pinned key
  keyid:  5aafdec2731ca7e9b0823690b17654f61e023df4f16c57518221b1cbc80936fc
  digest: 860255ee43404950287b2ca802e89c516cd378d6a9c739568911ca0cad8ab0ba
  manifest: checked against 1b0d099c64fa735ec55e85e8fc6fcd6c4aba122da238803970c10955719385ce
  chain: carried, NOT checked
```

!!! success "Check"
    The first line says `pinned key` and `manifest:` says `checked`. Only
    these two tell you *who* signed and *under which policy*; the
    trust-on-first-use line only tells you the bundle is internally
    consistent.

## 5. Chain a second receipt

The worker's next submission cites the first bundle's digest as its anchor:

```console
$ ANCHOR=860255ee43404950287b2ca802e89c516cd378d6a9c739568911ca0cad8ab0ba LENGTH=2 bash mkspec.sh > spec2.json
$ bernstein receipt create spec2.json --signing-key worker.pem -o bundle2.json
✓ wrote signed bundle to bundle2.json
  digest: 3ec0160daded1ddd8c676fb3f45309926624c450c0e3ab988f9faae157a1e1c5
```

The maintainer checks it continues the bundle they last accepted:

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

!!! success "Check"
    All three of key, manifest and chain are reported as checked.

## 6. Watch verification fail

**Wrong predecessor.** The maintainer expected a different previous bundle:

```console
$ bernstein receipt verify bundle2.json --pubkey worker.pub.pem \
    --prev-digest 0000000000000000000000000000000000000000000000000000000000000000
✗ bundle verification failed:
    chain.anchor: anchor 860255ee43404950287b2ca802e89c516cd378d6a9c739568911ca0cad8ab0ba does not link to predecessor 0000000000000000000000000000000000000000000000000000000000000000
```

**Edited patch.** Change the patch inside the signed payload after signing:

```bash
python - <<'EOF'
import base64, json
env = json.load(open("bundle.json"))
stmt = json.loads(base64.b64decode(env["payload"]))
stmt["predicate"]["bundle"]["patch"] = stmt["predicate"]["bundle"]["patch"].replace("a + b", "a + b + 1")
env["payload"] = base64.b64encode(json.dumps(stmt).encode()).decode()
json.dump(env, open("edited.json", "w"))
EOF
```

```console
$ bernstein receipt verify edited.json --pubkey worker.pub.pem
✗ bundle verification failed:
    envelope: signature verify failed for keyid='5aafdec2731ca7e9b0823690b17654f61e023df4f16c57518221b1cbc80936fc':
```

**Wrong key.** A genuine bundle, but not from the worker you expected:

```console
$ openssl genpkey -algorithm ed25519 -out other.pem
$ openssl pkey -in other.pem -pubout -out other.pub.pem
$ bernstein receipt verify bundle.json --pubkey other.pub.pem
✗ bundle verification failed:
    envelope: signature verify failed for keyid='5aafdec2731ca7e9b0823690b17654f61e023df4f16c57518221b1cbc80936fc':
```

!!! success "Check"
    Each of the three exits `1` (`echo $?`). `--json` returns the same
    verdict as `{"ok": false, ..., "errors": [{"field": ..., "message": ...}]}`
    for scripts.

## What you built

- An Ed25519 worker identity and a signed bundle that seals a patch, its
  test log and the policy digest.
- A two-link receipt chain, verified with the worker key, policy and
  predecessor all pinned.
- Three failures a review gate should reject: wrong predecessor, edited
  content, wrong signer.

## Next

- Wire `bernstein receipt verify --json` into your merge gate:
  [Signed results from workers you do not control](../use-cases/signed-worker-results.md).
- Every flag and the full verification order:
  [`bernstein receipt` reference](../reference/receipt.md).
