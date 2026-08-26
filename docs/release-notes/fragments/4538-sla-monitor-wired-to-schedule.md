---
SLA monitor integrated with `bernstein schedule run`
---

- Automatic SLA contract evaluation now runs on each supervisor tick.
- Breach receipts are signed and persisted for auditability.
- Chain events of type `sla.violation` are emitted for downstream processing.
- Trigger events are normalized to a consistent schema for downstream consumption.
