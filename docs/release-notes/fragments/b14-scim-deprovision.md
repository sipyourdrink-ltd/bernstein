## SCIM deprovisioning is reachable by admins and safe to repeat

`DELETE` and `PATCH` on `/scim/v2/Users/{id}` required `scim:write`, but no
role held it, so an SSO or JWT admin got 403 on the routes that
`ServiceProviderConfig` advertises. ADMIN now holds `scim:write`; OPERATOR and
VIEWER still get 403.

The principal ledger now takes a per-journal lock (in-process and `flock`)
around the read-tail-then-append step. Concurrent deprovisions no longer record
the same `record_index` and `prev_hmac`, so the signed chain stays linear and
keeps verifying.

A deprovision of a principal that is already revoked answers 404, like `GET`,
and signs no second record or audit event; the ledger itself skips a repeat
deprovision when asked. The ledger record is written before the identity is
revoked, so a request retried after a failed revoke completes without signing
twice.
