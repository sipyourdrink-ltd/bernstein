## A real signed record now exercises RFC 8785's UTF-16 key-ordering rule

`tests/fixtures/agent-card-utf16-vector/` adds an agent identity card, signed
through the production `sign_agent_card` / `canonicalize_jcs` path, whose
`extensions` field carries a property name in the U+E000..U+FFFF range and
one starting with a supplementary-plane character -- the one case where RFC
8785's UTF-16 code-unit key ordering disagrees with the obvious code-point
shortcut. Every record this codebase emits today has schema-fixed ASCII
keys, so this property was previously provable only against hand-built
dicts in `tests/unit/test_canonicalize_jcs_key_order.py`, never against a
record a real signing path actually produced. `tests/unit/test_agent_card_utf16_vector.py`
checks both directions: the committed card's signature verifies, and a
signature minted over the same card canonicalised by code point instead is
rejected. No production behaviour changes (#5551).
