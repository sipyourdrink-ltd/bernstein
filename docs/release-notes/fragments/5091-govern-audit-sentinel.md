## govern audit: injectable failure sentinel for end-to-end detection testing

Adds an injectable audit failure sentinel to prove the detect -> record -> notify path end to end (#5091):

- Setting `BERNSTEIN_AUDIT_SENTINEL` (or `.sdd/audit_failure.sentinel`) forces the named check `sentinel:injected_failure` to report `measured, failed` with reason `InjectedAuditFailureSentinel`.
- The sentinel's presence is explicitly reported in run outputs (`[SENTINEL ACTIVE]`) so an active sentinel cannot be mistaken for an unnoticed organic failure.
- Audit runs record events in the lineage journal under `.sdd/lineage/govern-audit/events.jsonl`, and failed checks emit notifications (`[SENTINEL ALERT]`).
