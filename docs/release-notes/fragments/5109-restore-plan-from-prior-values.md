## A change receipt keeps the prior value, so the restore plan is read off the record

Every entry in a change receipt now records the value the target held
immediately before the change alongside the value written, and
`bernstein.core.govern.build_restore_plan` projects an apply receipt into the
plan that undoes it. Every restore value is copied from the receipt; the
environment is consulted for one purpose only, to refuse an entry whose target
drifted since the apply or could not be read, which an operator overrides per
entry. Two operators building a restore from the same receipt therefore get the
same plan whatever the environment looks like when they run it. The plan
carries the digest of the receipt it inverts, so a restore is tied to its apply
record without a separate index. The three receipt fields are additive and
optional, so receipts written before them still verify offline and the schema
version is unchanged. (#5109)
