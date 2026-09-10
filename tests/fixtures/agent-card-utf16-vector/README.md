# Agent-card UTF-16 property-order vector

One agent identity card, signed through the production
`bernstein.core.security.agent_card_signer.sign_agent_card` path, whose
open-membership `extensions` field carries two property names that sort in
opposite orders under RFC 8785 (UTF-16 code units) and under the obvious
code-point shortcut.

| Key | Code point | UTF-16 code units |
|---|---|---|
| `U+FFFF` | 65535 | `FFFF` |
| `U+1D11E` | 119070 | `D834 DD1E` |

Code-point order puts `U+FFFF` first. UTF-16 order puts `U+1D11E` first,
because its lead surrogate (`D834`) sorts below `FFFF`. This is the exact
pair `chernistry` measured when filing #5551.

| File | What it is |
|---|---|
| `agent-card-utf16-vector.json` | the card body (`AgentIdentityCard` `asdict`), JCS-canonicalised |
| `agent-card-utf16-vector.sha256` | the SHA-256 of those exact bytes, in `sha256sum` format |
| `agent-card-utf16-vector-signature.json` | the detached JWS `sign_agent_card` produced over the card body, plus `kid`/`alg` |
| `agent-card-utf16-vector-key.pem` | the public key the signature verifies against |
| `_build_agent_card_utf16_vector.py` | the generator; the vector is never hand-edited |

Verify the digest by hand from this directory:

```
shasum -a 256 -c agent-card-utf16-vector.sha256
```

## Why a real card, not a hand-built dict

`tests/unit/test_canonicalize_jcs_key_order.py` already proves the
canonicaliser sorts these two code points correctly, against dicts built for
that test alone. That is necessary but not sufficient: every record this
codebase actually emits today has schema-fixed ASCII property names, so a
producer that took the code-point shortcut instead of RFC 8785's UTF-16 rule
would still pass every corpus we have, and only diverge from an independent
implementation the first time a key like this one reached a real signing
path.

`AgentIdentityCard.extensions` is where that key can legitimately appear: it
is typed `dict[str, str | bool | int | float]` with no schema restricting
its key names to the "recognised keys today" the field's docstring lists.
This vector's card sets `extensions` to a normal recognised key
(`task_budgets`) plus the two keys under test, then goes through
`sign_agent_card` unmodified -- the same function every real agent card is
signed with.

`tests/unit/test_agent_card_utf16_vector.py` checks both directions: the
committed card re-encodes to its own bytes and its signature verifies, *and*
a signature minted over the code-point-ordered bytes for the same card is
rejected by the same verifier. A canonicaliser that silently regressed to
code-point order would still pass every other test in the suite and only
fail here.

## Regenerating

```
uv run python tests/fixtures/agent-card-utf16-vector/_build_agent_card_utf16_vector.py
```

Every input is a constant -- pinned signing seed, fixed timestamps -- so
running it twice produces byte-identical output;
`test_regenerating_the_fixture_is_byte_identical_to_the_committed_files`
enforces that by calling the generator against a temporary directory.

The signing key is a test key, published here alongside the vector it
signs. It is not an installation identity and must never become one.
