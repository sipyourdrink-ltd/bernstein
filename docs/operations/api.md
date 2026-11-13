# Task REST API operational notes

## Task classification compatibility

`Task.to_dict()` includes `sensitivity`, and `Task.from_dict()` preserves
the supported values `public`, `internal`, and `confidential`. JSONL task
replay also retains an explicitly stored classification. Legacy task
payloads and JSONL records without the key use the existing `internal`
default; an omitted key does not establish a historical classification.

## Approval policy availability in responses

Current task responses include `approval_spec`: a JSON object contains the
current domain task's specification, while a present JSON `null` serializes
its current `None` value. That null does not prove that a legacy journal
originally recorded an absent policy. This field alone does not establish the
task's complete effective approval policy or grant governance mutation rights.

Legacy responses may omit `approval_spec`. Clients must preserve raw key
presence if they need to distinguish an unavailable field from an explicit
`null`; they must not infer that an omitted field proves no approval is needed.
When parsing with `TaskResponse`, both cases produce `approval_spec is None`,
but `"approval_spec" in response.model_fields_set` distinguishes a present
field from an omitted one. Default JSON reserialization includes the field
as `null` even when the original response omitted it, so a normalized payload
alone loses that distinction. This is existing compatibility behavior, not
a new availability marker or a guarantee about other client models.

The [API compatibility tests](https://github.com/sipyourdrink-ltd/bernstein/blob/main/tests/unit/test_api_compat.py) cover a
non-default specification, an explicit `null`, and a legacy response without
the key. The [fresh-store HTTP regression](https://github.com/sipyourdrink-ltd/bernstein/blob/main/tests/unit/tenant_scope/test_tenant_selection.py)
checks authorized claim/detail readback after replaying the complete journal.

## `GET /tasks` response cap

`GET /tasks` returns a bounded response in both shapes it supports.

### Paginated mode (recommended)

When the request includes `limit` or `offset`, the response is the
paginated envelope:

```json
{
  "tasks": [ ... ],
  "total": 12345,
  "limit": 100,
  "offset": 0
}
```

- `limit` is clamped to `[1, 500]`.
- `offset` is clamped to `>= 0`.
- `total` is the unpaginated count of matching tasks.

### Legacy mode (deprecated)

When neither `limit` nor `offset` is present, the response is a plain
JSON array of task objects. To avoid unbounded responses on stores with
thousands of tasks, this path now applies a hard cap of `500` items and
sets the following response headers:

| Header          | Value                                                                       |
| --------------- | --------------------------------------------------------------------------- |
| `Deprecation`   | `true`                                                                      |
| `Link`          | `</tasks?limit=500&offset=0>; rel="successor-version"`                      |
| `X-Total-Count` | total number of matching tasks (may exceed the returned array length)       |
| `Warning`       | `299` notice emitted only when truncation occurs                            |

Clients that rely on receiving the full dataset must migrate to the
paginated envelope by passing explicit `limit` and `offset` values. The
legacy shape will be removed in a future release.

### Choice rationale

We picked the silent-truncate path (rather than a `400 Bad Request`)
because there are in-tree callers (orchestrator, GUI, sync, planner)
that issue `GET /tasks` without pagination and which would otherwise
hard-fail mid-cycle. Truncation plus deprecation headers gives those
callers a deterministic upper bound on response size today and a clear
migration signal for tomorrow.
