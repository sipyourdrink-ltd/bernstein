## Agent principals gain a documented provisioning and deprovisioning lifecycle

Scoped per-task grants are now bound to an agent principal that has a signed,
HMAC-chained lifecycle record. Provisioning and deprovisioning are append-only
chain events; a principal that has been deprovisioned ends any grant whose
`principal` field names it, and a grant that names a principal with no chain
record at all is refused. Existing grant records (those written before this
change) are unaffected — their `principal` field is empty and the principal
chain is consulted only when it is present. The SCIM 2.0 directory adapter
(`bernstein.adapters.directory.scim`) maps standard provisioning resources
onto the lifecycle API. (#4972)
