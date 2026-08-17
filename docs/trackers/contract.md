# Tracker adapter contract

Trackers are external task sources (Linear, Jira, GitHub Projects v2,
GitLab, ClickUp, Asana, Plane, ServiceNow, or an enterprise's private
tracker). Bernstein consumes them through a single abstract contract so
the orchestrator, federation layer, audit log, and cost cap reason
about every tracker uniformly.

This page documents the contract surface and walks through the minimum
viable adapter.

## Surface

| Module | Contents |
|--------|----------|
| `bernstein.core.trackers.contract` | `AbstractTrackerAdapter`, dataclasses, exceptions |
| `bernstein.core.trackers.registry` | `TrackerRegistry`, `register_tracker`, plugin discovery |
| `bernstein.plugins.hookspecs` | `provide_tracker_adapter` hookspec |
| `bernstein.cli.commands.trackers_cmd` | `bernstein trackers list` / `trackers test` |

## Required methods

| Method | Required | Description |
|--------|----------|-------------|
| `pull_open_tickets(filter)` | yes | Yields `Ticket` objects for the open queue. |
| `add_comment(ticket_id, body, *, idempotency_key)` | yes | Posts a comment. Replays with same key + body return original result. |
| `transition(ticket_id, status_id, *, idempotency_key, etag)` | yes | Moves a ticket to a new status. Stale `etag` raises `OptimisticConcurrencyError`. |
| `claim_ticket(ticket_id, agent_id, *, etag)` | optional | Marks a ticket as in-flight for `agent_id`. Default: `NotImplementedError`. |
| `attach_blob(ticket_id, blob, mime, *, idempotency_key)` | optional | Uploads a binary blob. Default: `NotImplementedError`. |

## Exceptions

| Exception | Meaning |
|-----------|---------|
| `OptimisticConcurrencyError` | `etag` precondition failed; reload the ticket. |
| `IdempotencyConflict` | Idempotency key reused with a different payload. |
| `RateLimited(retry_after=<seconds>)` | Back off before retrying. |
| `TrackerUnavailable` | Tracker is unreachable or 5xx. |

## Idempotency

Adapters should treat `idempotency_key` as authoritative whenever the
tracker's API supports it (e.g. Linear's mutation `clientMutationId`,
Stripe-style `Idempotency-Key` headers). Where the tracker does not,
the adapter must dedupe locally via a comment marker (the agent posts a
`<!-- bernstein-key: ... -->` marker) or a SQLite ledger keyed on
`(ticket_id, op, key)`.

## Etag semantics

`etag` is opaque. Adapters serialise whatever the tracker's API uses
for optimistic concurrency:

- GitHub: `If-Match` ETag header.
- Jira: `versionAtUpdate` field.
- Linear: GraphQL mutation versioning.
- ServiceNow: `sys_mod_count` column.

A `None` etag means "skip the precondition check"; callers that always
want concurrency control should refuse to act on tickets whose `etag`
is `None`.

## Minimum viable adapter

```python
from collections.abc import Iterator
from typing import Any

from bernstein.core.trackers.contract import (
    AbstractTrackerAdapter,
    CommentResult,
    Ticket,
    TransitionResult,
)


class AcmeTracker(AbstractTrackerAdapter):
    name = "acme"

    def __init__(self, *, base_url: str, token: str) -> None:
        self._base_url = base_url
        self._token = token

    def pull_open_tickets(self, filter: dict[str, Any] | None = None) -> Iterator[Ticket]:
        # Call out to Acme's REST API, yield normalised Ticket objects.
        ...

    def add_comment(
        self,
        ticket_id: str,
        body: str,
        *,
        idempotency_key: str | None = None,
    ) -> CommentResult: ...

    def transition(
        self,
        ticket_id: str,
        status_id: str,
        *,
        idempotency_key: str | None = None,
        etag: str | None = None,
    ) -> TransitionResult: ...
```

## Shipping the adapter as a plugin

Out-of-tree adapters register through the `provide_tracker_adapter`
pluggy hook. The plugin returns a `TrackerRegistration`, a
`(name, factory)` tuple, or a list of either; the registry coerces the
shape.

```python
from bernstein.core.trackers.registry import TrackerRegistration
from bernstein.plugins import hookimpl


class AcmePlugin:
    @hookimpl
    def provide_tracker_adapter(self):
        return TrackerRegistration(
            name="acme",
            factory=AcmeTracker,
            summary="Acme tracker REST adapter.",
            capabilities=("comment", "transition"),
        )
```

Register the plugin via the `bernstein.plugins` entry-point group or
the `plugins:` list in `bernstein.yaml`. Once loaded:

```
$ bernstein trackers list --source plugin
NAME   SOURCE  CAPABILITIES         SUMMARY
acme   plugin  comment,transition   Acme tracker REST adapter.
```

## Smoke-testing an adapter

`bernstein trackers test <name>` constructs the adapter, calls
`pull_open_tickets` once with an empty filter, and reports the result.
The command is read-only: it never claims, comments, transitions, or
attaches.

```
$ bernstein trackers test acme --limit 1
tracker: acme  (plugin)
status : ok
fetched: 1 (limit 1)
```

When the adapter's factory requires constructor arguments that are not
present in `bernstein.yaml`, the command reports `status: skipped` with
the missing-argument error rather than failing. This makes the same
invocation safe to run in ephemeral CI environments.

## Reference fake

`tests/fixtures/trackers/in_memory_tracker.py` ships a deterministic
in-memory implementation used by every tracker unit test. The fake
mirrors the contract (etag bookkeeping, idempotency ledger, rate-limit
injection, unavailability toggle) so new adapters can plug into the
existing contract test suite by parameterising over the fake's API
sequence.

## Default-off behaviour

No tracker adapter is enabled until it is named in
`bernstein.yaml: trackers.enabled = [...]`. The registry only exposes
adapters that have been registered; enabling is the operator's
explicit step.
