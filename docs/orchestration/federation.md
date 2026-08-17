# Multi-tracker federation

## TL;DR

| Component | Purpose |
|-----------|---------|
| `FederationConfig` | Typed view over `bernstein.yaml: federation`. |
| `LinkDetector` (+ 3 defaults) | Find cross-tracker refs in ticket bodies, fields, comments. |
| `FederationBuilder` | Walks every adapter once and builds the per-run graph. |
| `FederatedTicketGraph` | Small in-memory graph rendered as agent-run context. |
| `FederationDispatcher` | Reads/comments/transitions with per-role allow-list and audit. |
| `CrossTrackerAuditRecord` | JSONL row recording every cross-tracker action. |

Vendor tracker AI is tenant-locked. The federation layer lets one
Bernstein orchestrator instance pull tickets from Linear, GitHub
Projects, Jira, Notion, etc. in a single run, detect cross-references,
and dispatch an agent that can act across the joined surface, with
every cross-tracker action recorded in the audit log.

## Configuration

Add a `federation` block to `bernstein.yaml`:

```yaml
federation:
  linked_trackers:
    - linear
    - github_projects
    - notion
  link_detectors:
    - url
    - custom_field
    - comment_mention
  cross_tracker_dispatch:
    allow:
      backend:
        - github_projects
      reviewer: "*"
```

Field reference:

| Key | Type | Meaning |
|-----|------|---------|
| `linked_trackers` | list of strings | Adapter names participating in this federation. |
| `link_detectors` | list of strings | Detector names to enable. Defaults to all three. |
| `cross_tracker_dispatch.allow` | mapping role -> trackers | Per-role write allow-list. `"*"` grants every tracker. |

A role missing from the allow-list is deny-all for cross-tracker
writes. Reads are never gated, but they still produce an audit entry
so cross-tracker information flow remains traceable.

## Worked example: Linear + GitHub Projects + Notion

Three adapters are configured. A Linear ticket links to a GitHub
Projects issue in its body, which in turn carries an `External-Link`
custom field pointing at a Notion page.

```python
from pathlib import Path

from bernstein.core.orchestration.federation import (
    FederationBuilder,
    FederationConfig,
    FederationDispatcher,
    write_audit_record,
)

builder = FederationBuilder(adapters=[linear, github_projects, notion])
graph = builder.build()
print(graph.render_context())
```

Rendered output:

```
federation:
  - node: github_projects:42
    -> notion:abc (custom_field: https://notion.so/p/abc)
  - node: linear:LIN-1
    -> github_projects:42 (url: https://github.com/acme/repo/issues/42)
  - node: notion:abc
```

The agent run consumes the rendered block as a single context message.
Tool calls into the dispatcher are namespaced by tracker:

```python
cfg = FederationConfig.from_dict(yaml_block)
sink = []
dispatcher = FederationDispatcher(
    graph=graph,
    adapters={
        "linear": linear,
        "github_projects": github_projects,
        "notion": notion,
    },
    config=cfg,
    role="backend",
    audit_sink=lambda record: write_audit_record(record, Path(".sdd")),
)
dispatcher.comment("github_projects", "42", "linked from LIN-1", from_tracker="linear")
```

The dispatcher routes the comment to the correct adapter, records the
`link_kind` derived from the graph (here `url`), and appends one
JSONL row to `.sdd/lineage/cross-tracker-audit.jsonl`.

## Default detectors

| Detector | Triggers on | Notes |
|----------|-------------|-------|
| `URLDetector` | `tracker_uri_base` prefix found in body or comments. | Skips URLs that point at the source tracker. |
| `CustomFieldDetector` | Custom-field keys named like `External-Link`, `links`. | Case-insensitive on field names. |
| `CommentMentionDetector` | Prefix-style ids (`JIRA-1234`, `LIN-456`) and optionally bare `#1234`. | Bare-hash mode requires `hash_default_tracker`. |

Detectors are side-effect-free, may be enabled or disabled per project,
and additional detectors can be passed to `FederationBuilder(detectors=...)`
without touching the core code path.

## Audit record shape

Every cross-tracker action emits one `CrossTrackerAuditRecord`:

| Field | Description |
|-------|-------------|
| `event` | One of `cross_tracker_read`, `cross_tracker_read_miss`, `cross_tracker_comment`, `cross_tracker_transition`, `cross_tracker_write_blocked`. |
| `timestamp` | Wall-clock seconds. |
| `role` | Agent role driving the dispatcher. |
| `tracker_name_from` | Tracker context the agent was reasoning from. |
| `tracker_name_to` | Adapter that received the call. |
| `ticket_id_from` | Source ticket id, if any. |
| `ticket_id_to` | Destination ticket id. |
| `link_kind` | Detector kind that justified the action, joined with `+` if multiple. |
| `action` | `read`, `comment`, or `transition:<state>`. |
| `detail` | Free-form note (e.g. `"adapter read-only"`). |

Records land in `.sdd/lineage/cross-tracker-audit.jsonl` by default and
can be tailed alongside the lineage merge audit, which uses the same
field naming convention.

## Permission boundary

The dispatcher consults `FederationConfig.is_allowed(role, tracker)`
before any write. A blocked attempt raises `FederationPermissionError`
and produces no audit record, since no action took place. A write that
reaches a read-only adapter raises `TrackerReadOnlyError` and emits a
`cross_tracker_write_blocked` audit row with `detail="adapter
read-only"`. The federation test suite exercises both paths and a
chained three-tracker setup.

## Out of scope (v1)

- Auto-discovery of which trackers a team uses.
- Cross-tracker ticket creation.
- Bi-directional link sync.
- Cross-tenant federation across organisations.
