# Run archive: what it contains, and how to prove it

An unattended run on an ephemeral workspace — a CI runner, a container, a
per-issue clone — deletes the workspace when the run ends. The evidence that
would explain a failure goes with it. `bernstein run archive` is what survives,
so for a postmortem the archive *is* the evidence, and evidence that cannot be
checked is a claim.

## What an archive contains

Sections are defined in `ARCHIVE_SECTIONS` (`src/bernstein/cli/run_archive.py`)
and every one of them is collected unless you name a subset:

| Section | Paths |
|---|---|
| `tasks` | `.sdd/tasks/*.jsonl` |
| `logs` | `.sdd/runtime/*.log` |
| `costs` | `.sdd/runtime/costs/*.json` |
| `audit` | `.sdd/audit/*.jsonl` |
| `metrics` | `.sdd/metrics/*.jsonl` |
| `traces` | `.sdd/traces/*.json` |
| `config` | `.sdd/config/*`, `bernstein.yaml` |

Each file is stored under its POSIX-style path relative to the project root
(`/`-separated on every host, so the same tree produces the same archive), and a
`manifest.json` is written at the archive root.

## The manifest is the contract

`manifest.json` carries `created_at`, `bernstein_version`, `run_id`,
`file_count`, `total_size_bytes`, the `sections` requested, and **`files`**: one
entry per archived member with the sha256 of its bytes.

`files` is what makes the rest checkable. A count and a byte total say how much
was collected and nothing about *what*, so an archive that lost a file in
transit, or had one edited afterwards, read as intact.

The manifest travels inside the archive, so verification needs nothing but the
file — which matters, because the workspace it describes has usually been
deleted by the time anyone looks.

## Verifying

`verify_archive(path)` re-hashes every member and compares it to the manifest.
It reports three failures separately, because they are different facts and lead
to different actions:

| Field | Meaning |
|---|---|
| `modified` | The member is present and its bytes no longer hash to what was recorded — evidence that changed after the fact. |
| `missing` | The manifest lists it and the archive does not hold it — evidence that did not survive. |
| `unexpected` | The archive holds it and no hash covers it — a member nothing vouches for. |

`ok` is `True` only when all three are empty.

An archive written before per-file hashes existed reports
`unverifiable` rather than `ok: True`. Nothing was checked, and "nothing
failed" is not the same claim as "everything matched" — reporting success there
would be exactly the false assurance the hashes remove.

An archive with no `manifest.json`, or one that cannot be parsed, is not a JSON
object, or lacks a required field, raises `ArchiveManifestError`. It is
deliberately not treated as an empty manifest: that would verify clean having
checked nothing. A manifest whose `file_count` disagrees with the number of
hashes it lists contradicts itself, and reports `unverifiable`.

## What the hashes do not prove

Per-file hashes detect modification; they do not attest authorship. The
manifest travels inside the archive with nothing outside it to check against, so
someone who can edit a member can also recompute its hash and rewrite the
manifest. Whether the manifest should be anchored or signed is tracked in
[#6258](https://github.com/sipyourdrink-ltd/bernstein/issues/6258).
