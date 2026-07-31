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

## Graded verdict: pass, fail, unproven

`valid` cannot separate a chain whose narrowing was checked and held from a
chain that recorded no scope to check. Both reach the caller as `True`, because
in neither case did a check find anything. `ChainResult.verdict` is the
additive surface that draws the line, and it never changes what `valid` means.

Each hop gets one row: `pass`, `fail`, or `unproven`. The chain composes them,
fail dominating, then any unproven making the chain unproven, then pass. Every
hop is evaluated and nothing short-circuits, so a widening late in the chain is
still found when an earlier hop is unproven. The chain carries an
`unproven_hops` count at the top level, so a ten-hop chain with nine unproven
hops cannot present as green.

The reason strings are a closed set. Fail: `axis_widened`,
`axis_widened_vs_ancestor`, `scope_ref_conflict`, and `chain_invalid` on the
chain. Unproven: `scope_missing`, `scope_ref_only_unresolved`,
`parent_receipt_unavailable`, `parent_scope_unavailable`,
`comparison_axis_unsupported`, `root_claimed_mid_chain`, and
`no_scope_recorded` on the chain. A root hop is reported with `is_root` and
`root_structural_only`, never as a narrowing pass, because it has no ceiling to
narrow against.

These reason strings belong to `ChainVerdict` rows and the chain verdict. The
violation names earlier on this page, `scope_ref_mismatch` and
`unscoped_parent`, belong to `AuthorityReport.violations` and its `check`
field; the two vocabularies are separate and never interchangeable. Where they
meet they differ by design. The authority layer reports `unscoped_parent` as a
failure at the offending hop; the verdict climbs to the nearest scoped ancestor
and grades a hop `parent_scope_unavailable`, unproven, only when no ancestor
carries a scope. `scope_ref_conflict` is the verdict-side reading of the same
disagreement the authority check reports as `scope_ref_mismatch`: an inline
scope that does not match its recorded reference.

Root status is positional and is never taken from the receipt. `parent_ref`
sits in the signed body, written by the same party that writes the scope, so a
hop that named the genesis anchor could otherwise opt out of the comparison
entirely and collect a `pass` row for a scope nobody checked. Only a hop with
nothing before it in the supplied set is a root. A hop that claims otherwise
mid-chain gets `root_claimed_mid_chain` and is unproven: it may be a second
tree's root or it may be evading its ceiling, and the receipts do not say
which.

Two observations are recorded without changing a verdict:
`scope_ref_unresolved_inline_governs` when an inline scope sits beside a
reference that could not be resolved, and `scope_ref_only_resolved` when the
comparison came from a resolved reference rather than inline bytes.

When a hop's direct parent records no scope, the child is compared against the
nearest ancestor that does. Subset-of-ancestor is a necessary condition under
transitive narrowing, so a widening found across the gap is evidence about the
far side of the gap and fails with `axis_widened_vs_ancestor`. When no ancestor
carries a scope, the hop is `parent_scope_unavailable` and unproven rather than
failed. A chain that records no scope anywhere never reaches this path.

What a pass does not establish: that runtime enforcement matched the recorded
scope, including consumption state such as remaining uses; that any grant was
appropriate policy; that the supplied receipt set is complete, or that no
alternate delegation path exists; that an unresolved reference would have
matched; anything about execution outcomes. unproven is not a pass, and pass is
the only positive claim.

`ChainResult.valid` keeps its compatibility meaning throughout: it reports
structural validity and stays `True` for an unscoped legacy chain whose verdict
is unproven. A caller that wants a positive narrowing claim checks
`verdict == "pass"`, never `valid` alone.

## Compatibility

Every field is optional. A chain recorded before these fields existed carries no
scope, produces no violations, and hashes byte-identically to before — the
optional keys are omitted from the signed body entirely when unset, so an old
writer and a new writer that record the same unscoped hop produce the same HMAC.
