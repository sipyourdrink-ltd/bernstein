## A jcs-v2 journal verifies and resumes

An `EventJournal` opened with the `jcs-v2` hash profile stamped each row with a
`hash_profile` field after hashing the payload, while verification, state
rebuild and resume hashed every non-envelope row key, `hash_profile` included.
The two payloads differed, so every row of such a journal failed with
`event_hash mismatch` at step 0 and the journal could not be resumed.

The writer now hashes the same projection the verifier recomputes, so the
profile is part of what the chain covers. A journal written under `jcs-v2`
verifies, rebuilds to its own head and resumes; rewriting a row's
`hash_profile` breaks verification. Journals under the default `py-json-v1`
profile are unchanged and keep verifying (#6358).
