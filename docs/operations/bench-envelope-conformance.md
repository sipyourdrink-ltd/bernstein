# Benchmark-envelope conformance

`self-conformance-bench-v1` runs five deterministic fixtures against the actual
offline `BenchVerifier`. Each probe first requires an untouched signed control
to verify as `MATCH`. The negative case must produce its specific rejection and
task results; an always-rejecting or always-accepting verifier earns no pass.

| Probe | Expected verification result |
| --- | --- |
| Receipt byte flip, retaining the stored receipt hash | `DIVERGED`, with a receipt hash mismatch |
| Reordered signed task results | `UNSIGNED`, with both task replays matching |
| Deleted task result, with a valid fixture signature | `COVERAGE_MISMATCH` |
| Receipt reused for another explicit fixture context, with valid fixture hashes and signature | `DIVERGED`, with a fabricated verdict |
| Valid bundle checked after removing its fixture signer from the trust map | `UNSIGNED` |

The fixture content hash is recomputed after mutations so the tests exercise
the verifier beyond the serialized loader's separate bundle-hash check. The
context probe uses a small typed fixture adapter; it does not claim to replay
an actual audit journal or prove production run identity. The last probe
measures signer trust removal, not key rotation within a chain.

Run and verify through the ordinary signed-bundle path:

```bash
bernstein bench run self-conformance-bench-v1 --out conformance.json
bernstein bench verify conformance.json --suite self-conformance-bench-v1 \
  --trusted-key FINGERPRINT=operator-public-key.pem
```

The outer bundle uses the configured installation signer; the verifier needs
an independently trusted public-key pin. The inner fixtures use a documented
public throwaway Ed25519 key and fixed timestamp zero. That key is never an
installation trust root and proves no producer identity. `--stub-signer` on
both commands is available for test-grade outer bundles only. Offline replay
scores embedded canonical fixtures and never executes tasks or calls a model.

The five-probe pass rate measures this benchmark-envelope fixture set only.
It is not evidence of complete audit/journal tamper detection, chain key
rotation, runtime isolation, chaos tolerance, cross-host determinism, or
release admission. Those parts of #5451 remain open. The suite declares
`CTL-ROB-01` as a measurement association, not a compliance certification.
