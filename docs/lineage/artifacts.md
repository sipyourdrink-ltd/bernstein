# Artifact keys - naming what a fleet produces

Lineage records are keyed by an **artifact key**. Historically that key was a
repo-relative path and nothing else, so the moment an output left the worktree
it lost its provenance identity: a release PR, a published package or a
deployed docs page had no key the chain could answer questions about.

An artifact key is now either a repo-relative path (unchanged, and still the
default) or a canonical URI from a **closed** scheme set.

## TL;DR

| Question | Answer |
|---|---|
| What is a key? | A repo-relative POSIX path, or `pr://` / `pkg://` / `deploy://` / `doc://`. |
| Is the scheme set open? | No. An unknown scheme is rejected at the write boundary, not stored. |
| Do old records still work? | Yes. A bare path is the canonical form of the implicit `repo` scheme; nothing is migrated or rewritten. |
| Case sensitivity? | Scheme and authority fold to lowercase; path segments keep their case. |
| Who validates? | `bernstein.core.lineage.artifact_uri`, used by both lineage write boundaries. |
| How do I inspect one? | `bernstein artifact list` / `log <uri>` / `health <uri>`. |
| Can the dashboard disagree with the CLI? | No. Both call one function and one serialiser; same state and instant give byte-identical JSON. |
| Does a write announce itself? | Yes. Every spine entry emits exactly one `artifact.produced` event, with no per-adapter opt-in. |
| What if a declared output never lands? | An attempt record goes on the chain under that key, so "tried and failed" is not the same answer as "nothing was ever scheduled". |

## The grammar

```
pr://<host>/<project-path…>/<number>      pr://github.com/acme/widget/2559
pkg://<ecosystem>/<name…>/<version>       pkg://pypi/bernstein/3.9.0
deploy://<environment>/<target…>          deploy://prod/docs-site
doc://<host>/<path…>                      doc://bernstein.example/lineage/artifacts
<repo-relative path>                      src/bernstein/core/lineage/spine.py
```

Rules that apply to every key:

- **One spelling per artifact.** The write boundary accepts only the canonical
  form. `PKG://PyPI/x/1.0`, `pkg://pypi/x/1.0/` and `repo://src/a.py` are all
  refused with their canonical form named in the error. Accepting two spellings
  would fork one artifact's history across two chains; rewriting silently would
  change the key an entry hash was already computed under.
- **No percent-encoding, no query, no fragment, no userinfo.** Two encodings of
  one byte would be two keys for one artifact.
- **No traversal.** A `..` segment is refused in a URI exactly as in a path.
- **Deterministic.** Canonicalisation is a pure function of the input string:
  no filesystem, no environment, no host paths. The same declared output yields
  the same key on any machine.

`repo://<path>` is accepted as an *input alias* whose canonical form is the
bare path, so an operator can write it in a declaration - but it is never a
valid on-wire key.

## External artifacts are anchored by reference

An external artifact's bytes do not live in the worktree, so the entry cannot
hash them directly. It anchors them the way C2PA anchors referenced content: the
entry's `content_hash` covers a small canonical document naming the artifact and
the digest it carried at decision time.

```python
from bernstein.core.lineage.artifact_uri import external_reference_content_hash

content_hash = external_reference_content_hash(
    "pkg://pypi/bernstein/3.9.0",
    digest="sha256:" + archive_sha256,      # the package archive
)
```

An external reference consumed as *text* (an HTML page, a PDF, a rendered
doc) reaches the agent through an extractor. Record the extractor identity
too, so the content address changes when the extraction changes:

```python
content_hash = external_reference_content_hash(
    "doc://example.test/lineage",
    digest="sha256:" + page_sha256,
    extractor="html-readability@2.4.1",     # stable "id@version", passed deliberately
)
```

A reference with no extraction step (a commit SHA, an image digest) must
omit `extractor`; an empty string is refused, because it would silently
change the content address of every such reference.

The digest must name its algorithm (`sha1`, `sha256` or `sha512`); a bare hex
string is ambiguous, and an ambiguous anchor is not an anchor. Entries recorded
this way carry `artefact_kind="external"`.

## Declared outputs

A task can state what it intends to produce, next to the evidence producers that
state how its work is verified:

```yaml
id: T-release
title: Cut the 3.9.0 release
role: devops
evidence_producers:
  - {name: tests, kind: test, command: [pytest, -q], required: true}
declared_outputs:
  - dist/*.whl
  - pkg://pypi/bernstein/3.9.0
  - doc://bernstein.example/releases/3.9.0
```

Entries may use `*`, `**` and `?`. `*` and `?` do not cross `/`; `**` does, and
`/**/` also matches zero intervening segments. The authority may not be globbed -
the set a declaration covers should be obvious from reading it. Declarations are
canonicalised, deduplicated and sorted at task construction, so two spellings or
two orderings of the same set produce the identical stored value.

At completion the gate computes a three-way diff and seals it into the evidence
bundle's **signed binding**:

| Bucket | Meaning |
|---|---|
| `declared_and_produced` | The intent was honoured. |
| `declared_but_missing` | Declared and not produced. Also written to the chain as an [attempt record](#attempt-records). |
| `produced_but_undeclared` | A write nobody declared - the classic symptom of an agent drifting off its brief. |

Because the diff lives inside the binding rather than beside it, removing a
finding invalidates the bundle's signature and its spine anchor:

```bash
bernstein evidence show <task>     # renders the diff when the bundle carries one
bernstein evidence verify <task>   # proves it offline; exit 2 on any tamper
```

Sealing stays fail-open: a malformed declaration or a projection error is logged
and swallowed, never allowed to fail a task that already completed. A bundle for
a task that declares no outputs carries no diff at all and canonicalises
byte-for-byte identically to a pre-feature bundle, so every signature and anchor
already on disk stays valid.

### Attempt records

The evidence bundle is keyed by *task*. An operator asking about the artifact -
"did anything try to publish this?" - is coming from the other direction, and a
declared output that never landed used to leave nothing at all under its key. So
the absence is recorded on the same chain as the presence.

When a run ends, every concrete declared output is looked up against that run's
spine. Anything missing gets an **attempt record**: an ordinary spine entry keyed
by the declared URI, whose `step_id` is `artifact-attempt:<outcome>:<task_id>`
and whose `content_hash` covers a canonical description of the attempt rather
than any artifact bytes.

| Outcome | Meaning |
|---|---|
| `failed` | The task did not reach a merged, accepted completion. |
| `incomplete` | The task was accepted, and a declared output is still absent. |

Being an ordinary entry, the record is Merkle-chained, HMAC-tagged and
tamper-evident per entry, so "task T tried and did not deliver" is as verifiable,
and as hard to backdate, as a record saying it did.

An attempt is never counted as a production. It does not raise
`production_count`, it can never be the tip, it is excluded from the keys a run
is observed to have produced - otherwise the record of a missing output would
satisfy its own declaration on the next run - and it fires no trigger, because a
downstream goal reacts to its upstream *landing*.

Glob declarations are skipped: `pkg://pypi/bernstein/*` names a set, so there is
no single key to record an attempt under. A declaration that wants an attempt
record has to name the thing it promises.

Reconciliation is fail-open. It runs after the task has already finished, so a
read-only spine or an unreadable key store costs a record and never a completion.

## Inspecting an artifact

Three commands answer the questions that used to mean correlating four surfaces
by hand. All three read local `.sdd` state and need no network.

```bash
bernstein artifact list                       # every key the local spines carry
bernstein artifact log  pkg://pypi/bernstein/3.9.0
bernstein artifact health pkg://pypi/bernstein/3.9.0
```

`log` is the attribution surface: newest production first, each record naming
the producing agent identity, the model it ran, the run and step it came from,
and the spine entry hash that proves it. `verified` is recomputed per entry, so
a tampered row is *named* rather than averaged into a summary. Recorded attempts
follow the productions, under `attempts` in the JSON form; an empty production
list beside a populated attempt list is the shape that says something tried and
did not deliver.

`health` rolls the picture up into one verdict:

| Leg | Passes when |
|---|---|
| `produced` | At least one spine entry *produces* the key. When nothing does, the detail says whether attempts are recorded against it or whether nothing ever tried. |
| `chain_integrity` | Every entry carrying the key recomputes its hash and HMAC tag, and the chains they sit in verify. |
| `single_open_tip` | Exactly one set of bytes claims to be current. |
| `evidence` | The newest sealed evidence bundle that declares the key verifies. |
| `cadence` | The last production is inside the declared refresh cadence. |

| Verdict | Meaning |
|---|---|
| `green` | Every applicable leg passes. |
| `amber` | Nothing is broken, the artifact is just out of date. |
| `red` | An integrity or currency failure. Exit code 2. |

A leg with nothing to say reports `not_applicable` and cannot hold the verdict
down. An artifact with no declared cadence is not "failing cadence", and one no
bundle references is not "failing evidence" - absence of a signal is never
reported as a negative signal.

### Reproducing a verdict

The verdict is a pure function of the collected state and the evaluation
instant, and the instant is an explicit argument on both surfaces:

```bash
bernstein artifact health pkg://pypi/bernstein/3.9.0 --at 1750000000 --output-json
curl 'http://localhost:8000/artifacts/health?uri=pkg://pypi/bernstein/3.9.0&at=1750000000'
```

Those two produce **byte-identical** bytes, because they are two callers of
`artifact_health_json` and it is the only place a verdict is serialised. Pin a
verdict from the dashboard, recompute it offline, compare with `diff`.

## Production events

Every artifact write that lands in the spine emits exactly one
`artifact.produced` event, journaled at `.sdd/lineage/<run_id>/artifact-events.jsonl`
beside the spine it projects from, and mirrored onto the SSE bus when a server
is attached. The emission point is the single lineage write boundary, so there
is no per-adapter opt-in to forget.

The event is a **pure projection of one spine entry**:

```json
{"actor":"agent-release","content_hash":"sha256:…","entry_hash":"sha256:…","model":"claude-opus-5","run_id":"run-7","step_id":"publish","timestamp":1750000000,"uri":"pkg://pypi/bernstein/3.9.0","v":1,"verified":true}
```

That purity is what makes the fan-out replayable. Re-deriving the events from
the spine reproduces the identical set, and comparing the two reports any
difference as a divergence naming the offending `entry_hash`:

| Divergence | Meaning |
|---|---|
| `dropped` | The spine implies a firing the journal never recorded. |
| `duplicated` | The journal fired one entry more than once. |
| `unexpected` | The journal carries a firing with no matching spine entry. |
| `altered` | Same entry, different payload - a tampered spine row or an edited journal. |

A byte flip anywhere in a spine row makes that artifact's event replay as
`verified: false` while the journaled row still claims `true`; the disagreement
surfaces as an `altered` divergence and the artifact's health verdict turns red.

Emission is deliberately **fail-open**. Recording into the spine is fail-closed
because provenance is a hard requirement; the event is a notification, and a
full disk or a dead subscriber must never turn a successful artifact write into
a failed one. Anything a failed emit dropped is rebuilt by replaying the spine.

## Reacting to an artifact

`bernstein.core.trigger_sources.artifact` normalises production events into the
same `TriggerEvent` every other source emits, so artifact rules go through the
existing rule matching with no second engine. The key rides in `changed_files`
(path-shaped filters see it unchanged) and the full projected payload rides in
`raw_payload`.

URI patterns select what fires - `pkg://pypi/bernstein/*` covers every version
of one package. An entry whose integrity verdict is false does **not** fire by
default: a firing caused by a tampered record is worse than a firing that never
happened.

## Compatibility

Existing records are **not migrated**. A bare repo-relative path is interpreted
as the implicit `repo` scheme and reproduced verbatim, so every historical entry
keeps its exact entry hash, HMAC tag and signature.

One behaviour tightened on purpose. A string containing `://` used to slip past
the repo-path checks - `ftp://evil/x` has no leading `/`, no drive prefix and no
`..` segment - and was stored verbatim as if it were a filename. Such a string
is now parsed as a URI and rejected unless its scheme is known. Repo-relative
paths, which by construction contain no `://`, are unaffected.
