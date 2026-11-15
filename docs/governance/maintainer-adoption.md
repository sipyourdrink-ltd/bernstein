# Immutable maintainer declarations

Maintainer adoption evidence is an immutable declaration accepted into the
protected default branch through ordinary review and the merge queue. This
format and offline reader do not grant a quorum exception. Activation needs a
separate reviewed policy change; no workflow or active gate consumes this
format in this slice.

## Custody

The authoritative store is `.github/maintainer-adoption-ledger.jsonl` on the
protected default branch. `.github/` already requires the maintainer's code
owner approval. Changes to the reader also require that approval. A declaration
is authoritative only after its ordinary review and queue acceptance. A PR's
copy of a ledger, a Git author string, a comment, or an arbitrary commit is not
proof of that acceptance. The operator pins an exact trusted default-branch
commit matching the current trusted `refs/remotes/origin/main` snapshot. The
reader rejects another branch or an older pin; trusted retrieval and freshness
of that local remote-tracking ref remain the operator's responsibility. It
never fetches from an untrusted repository or treats a candidate ref as main.

Each canonical JSON line is content-addressed by the SHA-256 of its `schema`,
`operation` and `payload`. `schema` is `bernstein.maintainer-adoption.v1`.
Existing bytes are immutable: every accepted first-parent revision must retain
the complete previous prefix. Edited, removed, truncated, noncanonical and
unavailable history fails closed for the entire ledger. A full repository
history is required; shallow copies cannot establish durable revocation.

An `adopt` payload has `subject`, `producer_execution_ids`, `reviews` and
`security_tests`. The subject binds repository, PR number, base/head SHA and
the trusted policy digest. At least two distinct review executions must cover
the exact head and differ from all producers. Each review envelope contains a
terminal report and its recomputable digest. Reports require `PASS`, no
findings and a substantive summary. A separate security-test report must name
executed checks. The report schema is the same for each: `execution_id`,
`head_sha`, `outcome`, `findings`, `checks`, `summary`.

Hashes establish content integrity. The maintainer's ordinarily reviewed
declaration attests execution identity and review provenance; the hashes do
not independently authenticate a model, reviewer or provider. No report text,
URL or command is executed by the reader.

## Durable revocation

Append a `revoke` record whose payload names `declaration_id` and a bounded
`reason`. The referenced accepted declaration remains present. Its repository,
PR and head revision become permanently revoked. Moving the base or changing
policy cannot reactivate that head, and a later declaration cannot silently
restore it. A new revision needs new ordinary review. Deleting a ledger or
rewriting an earlier line invalidates evidence rather than selecting an older
declaration.

Policy binding covers the reader, charter, roster, CODEOWNERS, quorum and queue
scripts, privileged check publisher, quorum workflows and this custody
contract. The ledger itself is excluded to avoid a digest self-reference.

## Offline inspection

```bash
python scripts/maintainer_adoption.py --repo . --trusted-commit <accepted-main-sha>
```

This prints counts and `grants_merge_authority: false`; it does not publish a
check, approve a PR, change rulesets or alter the existing quorum evaluator.
No declaration ledger or activation is installed by this change.
