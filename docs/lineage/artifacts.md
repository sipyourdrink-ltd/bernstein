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
| `declared_but_missing` | Declared and not produced - this is what makes "attempted and failed" distinguishable from "nothing was ever scheduled". |
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

## Compatibility

Existing records are **not migrated**. A bare repo-relative path is interpreted
as the implicit `repo` scheme and reproduced verbatim, so every historical entry
keeps its exact entry hash, HMAC tag and signature.

One behaviour tightened on purpose. A string containing `://` used to slip past
the repo-path checks - `ftp://evil/x` has no leading `/`, no drive prefix and no
`..` segment - and was stored verbatim as if it were a filename. Such a string
is now parsed as a URI and rejected unless its scheme is known. Repo-relative
paths, which by construction contain no `://`, are unaffected.
