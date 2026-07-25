# Delegation narrowing, separation of duties, and decision binding

`bernstein delegation verify <run>` reconstructs a run's per-hop delegation
receipts offline. This page covers what it can prove about *authority* —
beyond the tamper evidence the HMAC chain already gave you.

## Why sequence is not narrowing

A delegation receipt records `issuer`, `subject`, `audience`, and `act`, HMAC-
chained hop to hop. That proves the **sequence and integrity** of a delegation
chain: the order is fixed, no hop was deleted, no field was flipped.

It does not prove that authority **narrowed**, because the authority granted at
each hop was never part of the record. Without it, the only answer to "did this
sub-agent hold more than the worker that spawned it?" is "the route checks
would have refused anything wider" — which is an argument about code paths, not
something an auditor can recompute from the receipts they were handed.

A hop now records the **effective scope** it granted (or a content-addressed
reference to it) plus the **parent hop it descends from**, so the child ⊆ parent
relation is recomputed per hop from the receipts alone.

## Three checks, deliberately distinct

| Check | Question | Failure |
|---|---|---|
| Narrowing | Did a child hop gain authority its parent lacked? | `narrowing_ok: false`, offending axis named |
| Separation of duties | Did one principal exercise incompatible powers? | `duties_ok: false`, the duty pair named |
| Decision binding | Was a gated decision pinned to a charter version? | `binding_ok: false` |

Blurring them loses information. A chain can narrow perfectly at every hop and
still route both `spawn` and `approve` to the same principal. A chain can
separate duties cleanly while widening scope at one hop. Each check reports its
own verdict; `valid` is true only when the chain is intact **and** all three
pass.

### Narrowing axes

Scope axes follow the capability-token convention that `None` is the *widest*
value, so a child that drops a bound its parent imposed widens on that axis.

| Axis | Subset rule |
|---|---|
| `permissions` | set subset |
| `duties` | set subset |
| `task_ids` | allowlist subset (`None` = any task) |
| `path_prefixes` | POSIX ancestor-or-equal coverage (`/a/b` covers `/a/b/c`, never `/a/bc`) |
| `not_after` | no later than the parent |
| `max_uses` | no greater than the parent |
| `max_depth` | no greater than the parent |

The allowlist, path-prefix, and bound primitives are the same code that governs
signed capability tokens (`bernstein.core.security.capability_tokens`), so an
axis means one thing across both surfaces.

Four structural failures are reported with their own axis names:

- `scope_ref_mismatch` — the inline scope does not hash to the recorded content address.
- `unresolved_scope` — a by-reference scope could not be fetched.
- `unresolved_parent` — `parent_ref` names no preceding hop.
- `unscoped_parent` — the hop records a scope but its parent does not, so the ceiling it narrows against is unrecorded. Narrowing is then **unprovable**, which is reported as a failure rather than assumed to be fine.

### Separation of duties

Duties are `spawn`, `approve`, and `merge`. By default all three pairs are
separated: whoever asked for the work does not bless it, and whoever blessed it
does not land it. The acting principal is the receipt's `subject`.

Duties come from the hop's recorded scope when it declares any; otherwise they
are inferred from the `act` name on whole segments (`task.spawn` → `spawn`,
while `spawner.status` matches nothing), so chains recorded before scopes
existed still get coverage.

The rule set is an argument, not a fixed policy — pass your own pairs to
`check_duties(receipts, separated=...)`.

### Decision binding

A hop may record the charter hash and tenant-certificate version **in force
when it was recorded**. The binding sits inside the receipt body, so it is
covered by the receipt HMAC: rewriting a recorded version breaks the chain
rather than silently reinterpreting a historical decision.

Two failures:

- `binding_missing` — the chain binds a charter version somewhere, but a hop exercising `approve` or `merge` records none, so that decision floats free of the version it was taken under.
- `binding_drift` — a hop cites a different charter hash or certificate version than its parent. One authority chain is evaluated under one version; a changed charter needs a new certificate, not a re-interpreted hop.

## Recording a scoped hop

```python
from bernstein.core.identity.delegation import record_delegation_hop
from bernstein.core.identity.delegation_scope import DecisionBinding, DelegationScope

record_delegation_hop(
    run_id="run-42",
    issuer="orchestrator",
    subject="sub-agent:backend",
    audience="sub-agent:backend",
    act="task.delegate",
    scope=DelegationScope(
        permissions=frozenset({"files.read", "files.write"}),
        duties=frozenset({"spawn"}),
        task_ids=frozenset({"t-1"}),
        path_prefixes=frozenset({"/repo/src"}),
        not_after=1_800_000_000.0,
        max_uses=4,
        max_depth=2,
    ),
    binding=DecisionBinding(charter_hash="sha256:...", certificate_version="7"),
)
```

Pass `inline_scope=False` to record only the content address; the verifier then
needs a resolver:

```python
verify_run_chain(root=root, run_id="run-42", key=key, scope_resolver=store.get)
```

## Verifying

```console
$ bernstein delegation verify run-42
  hop 0: principal:alex -> orchestrator  (run.authorize)
      scope sha256:8f1c...
  hop 1: orchestrator -> sub-agent:backend  (task.delegate)
      scope sha256:2b90...
scope coverage: full
delegation chain intact (2 hop(s))
```

A widening hop exits non-zero with the axis named:

```console
$ bernstein delegation verify run-42
  ...
hop 1 (parent hop 0): narrowing/permissions: child scope widens the parent on
permissions: {files.read, gate.approve} is not within {files.read, files.write}
delegation authority verification failed: narrowing
```

`--json` emits the same verdict machine-readably, with `chain_ok` (tamper
evidence) separate from `valid` (tamper evidence plus all three authority
checks), an `authority` block carrying the three sub-verdicts, the scope
coverage, and every violation with its `check`, `axis`, `hop_index`,
`parent_hop_index`, and `principal`.

## Compatibility

Every field is optional. A chain recorded before these fields existed carries no
scope, produces no violations, and hashes byte-identically to before — the
optional keys are omitted from the signed body entirely when unset, so an old
writer and a new writer that record the same unscoped hop produce the same HMAC.
