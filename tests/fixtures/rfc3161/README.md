# RFC 3161 test fixtures

Real TimeStampToken + trust bundle fetched from FreeTSA
(<https://freetsa.org/>). Used by
`tests/unit/test_rfc3161_verifier.py` and
`tests/unit/test_audit_multitenant_v2.py` so the verifier exercises a
genuine RFC 3161 chain instead of a hand-rolled fixture.

| File                          | Origin                                         |
|-------------------------------|------------------------------------------------|
| `freetsa_payload.txt`         | The literal payload bytes that were timestamped. |
| `freetsa_token_with_certs.tsr` | TSA response (DER `TimeStampResp`) for the payload, requested with `-cert` so embedded TSA cert + root are present. |
| `freetsa_tsa.crt`             | FreeTSA leaf signing cert (also embedded inside the token). |
| `freetsa_cacert.pem`          | FreeTSA root CA — the trust anchor.            |
| `seal_anchor.json`            | v3.19.2 on-disk RFC 3161 anchor over that token. Used by `test_existing_rfc3161_anchor_files_still_load`. |

To refresh the fixtures (e.g. after the TSA rotates its key), run:

```bash
echo "Hello, World!" > tests/fixtures/rfc3161/freetsa_payload.txt
openssl ts -query \
    -data tests/fixtures/rfc3161/freetsa_payload.txt \
    -no_nonce -sha256 -cert \
    -out /tmp/_query.tsq
curl -sS -H "Content-Type: application/timestamp-query" \
    --data-binary @/tmp/_query.tsq \
    https://freetsa.org/tsr \
    -o tests/fixtures/rfc3161/freetsa_token_with_certs.tsr
curl -sS https://freetsa.org/files/cacert.pem \
    -o tests/fixtures/rfc3161/freetsa_cacert.pem
curl -sS https://freetsa.org/files/tsa.crt \
    -o tests/fixtures/rfc3161/freetsa_tsa.crt
```

The fixtures are byte-stable — the test suite does not run live network
calls during pytest. If FreeTSA is reachable, you can also run
`tests/unit/test_rfc3161_verifier.py::TestLiveTSAFetch` (gated behind
`pytest --live`) to fetch a fresh token at test time.
