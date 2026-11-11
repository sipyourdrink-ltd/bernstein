# Administrative container projection

`core/administrative_runs.py` projects organizational run membership and status
from the existing installation HMAC audit chain. It owns no planner, scheduler,
worker or background process. The task store calls the registry after durable
administrative task writes; status fails closed on chain or live-snapshot drift.
The REST routes share existing tenant/auth policy and the MCP layer explicitly
selects this surface for the administrative template. Ordinary run journals are
unchanged. The orchestrator excludes manual run members from automatic dispatch.

The store lock serializes mutations and the audit chain transaction serializes
projection/append across cooperating writers. Durability spans two existing
logs, not a new database transaction; interrupted writes are detectable and must
be reconciled before certification. See the operator workflow for receipt format.
