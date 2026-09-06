# UTF-16 key-ordering vector

A signed record whose canonical form distinguishes RFC 8785 key ordering from
the shortcut that sorts by Unicode code point. Issue #5551.

## The two orderings

RFC 8785 §3.2.3 sorts object property names as arrays of **UTF-16 code units**.
The obvious implementation sorts by **Unicode code point**. They agree for
every name below U+D800, so an ASCII or BMP-only corpus cannot tell them apart.

| Key | Code point | UTF-16 code units |
| --- | ---: | --- |
| `U+FFFF` | 65535 | `FFFF` |
| `U+1D11E` (MUSICAL SYMBOL G CLEF) | 119070 | `D834 DD1E` |

They diverge because a supplementary-plane character is encoded as a surrogate
pair whose first unit lies in `U+D800..U+DBFF` — numerically *below* `U+FFFF`,
even though its code point is far above it.

- **Code-point order:** `U+FFFF` first.
- **UTF-16 code-unit order:** `U+1D11E` first.

Opposite results, same two keys. This vector's canonical bytes put `U+1D11E`
first, which is the RFC 8785 answer.

## What is here

| File | What it is |
| --- | --- |
| `card.json` | The card body, exactly as `_card_to_dict` emits it |
| `signature.json` | The detached JWS produced by `sign_agent_card`, and its `kid` |
| `public-key.pem` | The verifying key |
| `public-key.jwk.json` | The same key as a JWK |
| `_build_utf16_ordering_vector.py` | Re-mints all of the above |

## Why the record comes from the production signer

A vector a codebase writes for itself only shows that the codebase agrees with
itself. The property under test is that an *independent* RFC 8785
implementation computes the same signing bytes we do, so the record has to be
one we genuinely emit. It is signed by `sign_agent_card` over `_card_to_dict`,
the path every signed card takes.

The keys live in `extensions`, the one open-membership map on
`AgentIdentityCard`. Every other signed field has a schema-fixed ASCII name,
which is precisely why this property was previously unfalsifiable from the
outside: our own corpus could not express a key that exercises it. **No schema
was changed** to admit these keys — the field is already
`dict[str, str | bool | int | float]`.

## Using it as a conformance vector

Canonicalise `card.json` under your own RFC 8785 encoder and compare against
the JWS payload in `signature.json` (base64url, unpadded). If your encoder
sorts by code point, the bytes will differ and the signature will not verify —
that is the point of the vector, not a defect in it.

## Reproducing

```sh
uv run python tests/fixtures/utf16-ordering-vector/_build_utf16_ordering_vector.py
```

The signing key seed and both timestamps are pinned, so a re-mint is
byte-identical. `tests/unit/test_utf16_key_ordering_vector.py` asserts the
ordering properties against the committed files.
