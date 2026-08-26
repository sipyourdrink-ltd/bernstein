An unresolved SLA breach is now attested once, not once per supervisor tick: the
monitor tracks the breach shape (contract hash plus breached axes) and emits a
new signed receipt, chain event and trigger only when the shape changes or the
breach resolves and recurs. A supervisor ticking at seconds no longer turns one
open breach into thousands of identical attestations.
