# Research activities: sourced reports with offline citation lineage

The deterministic scheduler dispatches any agent modality behind one typed
activity boundary. The research modality produces a **sourced report** whose every
claim is bound to the exact bytes it was derived from, so the report stays
checkable offline months after the run, with the network disabled.

A links-in-markdown report cannot offer this: a link still resolves after a page
is edited, moved, or rewritten, but to different bytes, and no reader can tell. A
research report here is not prose with links; it is a citation-lineage artifact.

## The citation model

Every claim in a report carries at least one **citation record**:

| Field | Meaning |
| --- | --- |
| `claim_id` | The claim the citation supports |
| `quote` | The exact span quoted from the source page |
| `source_ref` | Human-facing provenance (the page URL or label) |
| `page_content_hash` | `sha256:` hash of the fetched page, stored at fetch time |

The load-bearing field is `page_content_hash`. The research worker routes every
fetch through `ResearchActivity.fetch`, which content-addresses the page bytes
into the run content store the moment they land. The citation pins that hash, so
the claim is only satisfiable against the exact bytes it was drawn from.

The report itself is content-addressed: its canonical JSON bytes are stored in the
content store, and that hash is anchored as the `artifact_hash` of the
`ActivityResult` dispatched with `ActivityKind.RESEARCH`. The crossing is mirrored
into the HMAC-chained audit log, which records only hashes, never the report body.

## Boundary refusal: no uncited claims

A report with an **uncited** claim never reaches the journal. The worker validates
the assembled report before the result is built; a claim with no citation (or a
citation with an empty quote, a malformed page hash, or a source the worker never
fetched) is refused with `ActivityRejected` at the boundary.

```python
from bernstein.core.orchestration.research_worker import (
    ClaimDraft,
    ResearchBudget,
    ResearchWorker,
    SpanRef,
)

worker = ResearchWorker(store=store, budget=ResearchBudget(max_fetches=8))

def synthesise(query, fetched):
    # The opaque, stochastic step (a model in production). It drafts claims and
    # the spans they quote; the worker binds each span to the fetched page hash.
    return [
        ClaimDraft(
            statement="3.13 ships an optional free-threaded build",
            spans=(SpanRef(source_ref="https://example/a", quote="free-threaded build"),),
        ),
    ]

run = worker.run(
    query="what changed in 3.13",
    sources=["https://example/a", "https://example/b"],
    fetch_fn=fetch_bytes,       # retrieves the exact page bytes
    synthesise=synthesise,
)
dispatch_activity(run.result, stage_id="research-0", journal=journal, chain=chain)
```

Planning, fetching, budgeting, report assembly, and hashing are a pure function of
the query, the plan, and the fetched bytes. Two operators with the same inputs
assemble the byte-identical report and anchor the same `artifact_hash`. Only the
`synthesise` step is stochastic, and the worker admits nothing it emits unless the
quoted span resolves to a page the run actually fetched.

## Cost caps

`ResearchBudget` bounds how many sources a run may fetch and how much it may spend.
The cap is a first-class refusal, checked before every fetch: the worker raises
`ResearchBudgetExceeded` the moment a fetch would cross it, so a research task
scheduled next to coding tasks stays inside its budget the same way a coding task
does. Both modalities anchor into the same run journal through the one
`dispatch_activity` path.

## Offline verification

`bernstein activity verify <run>` resolves every citation from the content store
alone. For each claim, in report order, it reattaches each cited page's bytes by
its pinned hash, re-hashes them to detect an altered source, and confirms the
quoted span still occurs in them, emitting a per-claim verdict.

```console
$ bernstein activity verify run-42
Activity verify run=run-42
  OK research-0 [research] (evidence reattached, 2 citations resolved)
    cite OK claim=c1 (1 checked)
    cite OK claim=c2 (1 checked)
verified -- every activity reconstructs from the journal.
```

Alter a single stored source page and verification fails, naming the claim and the
mismatched hash:

```console
$ bernstein activity verify run-42
Activity verify run=run-42
  MISMATCH research-0 [research] -- claim 'c1' citation failed: cited page content hash mismatch (pinned 'sha256:...', recomputed 'sha256:...')
    cite FAIL claim=c1 (0 checked) -- claim 'c1' citation failed: cited page content hash mismatch (...)
    cite OK claim=c2 (1 checked)
```

Exit codes: `0` verified, `1` no run / no activity, `2` mismatch (tamper). The
check touches only the content store, so it holds with the network disabled, and
its output is a pure function of the report and the stored bytes: two verify runs
over the same completed run produce byte-identical verdicts.

## Why this shape

The report is not "prose plus an audit log". Strip the content store and the
report stops meaning anything, not merely stops logging: a claim's citation is an
unresolvable hash without the bytes behind it. The artifact the operator reads is
itself the verifiable receipt of the exact evidence each claim rests on.
