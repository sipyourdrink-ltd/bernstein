## Share legacy signing bytes across three baseline modules

Evidence bundles, governance decisions and review receipts now share their
existing JSON-byte helper. Existing import aliases, payload schemas, hashes
and signature bytes are preserved, including legacy float encoding. This is a
partial consolidation; other canonicalization formats remain unchanged (#5094).
