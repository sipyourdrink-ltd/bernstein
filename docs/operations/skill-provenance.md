# Skill usage provenance

The [skill catalog](skills-catalog.md) proves *what a registry claims* about
a skill — a signed manifest and a content digest. It does not, on its own,
tie an install to what the skill actually did. `bernstein skill provenance`
and `bernstein skill verify` add that usage-attestation layer on top of the
content-hash pinning already carried in `skills.lock`.

This is a different CLI group from `bernstein skills catalog` (plural
`skills`, browse/install/upgrade). `bernstein skill` (singular) only
inspects usage of an already-installed skill.

## Commands

```
bernstein skill provenance SKILL [--workdir DIR]
bernstein skill verify SKILL [--workdir DIR]
```

`SKILL` is either a catalog entry id (resolved to its installed content
digest via `skills.lock`) or a raw content digest.

### `provenance` — verified runs and artifacts a skill fed

Prints a table of every run the skill participated in, together with each
run's journal head and whether that head still verifies. Exit codes: `0`
graph rendered (may be empty when the skill has no recorded usage), `1` bad
input.

The verified-run count (`verified_runs` in the header line) is **recomputed
on every call** from the runs whose journal head still verifies and still
equals the head recorded at usage-link time — it is never read from a
stored counter. A run whose journal has since diverged or been tampered
with drops out of the count rather than inflating it.

### `verify` — recompute the install receipt

Recomputes the skill's install receipt and flags a `manifest_hash` that no
longer matches the currently installed content — a drift indicates a manual
edit under `.bernstein/skills/<name>/` or a rewritten install. Exit codes:
`0` verified (receipt anchored, manifest hash matches), `1` no receipt for
the skill, or the skill id has no matching row in `skills.lock` (pass a
catalog entry id installed via `bernstein skills catalog install`), `2`
mismatch (manifest hash drifted from the receipt).

## How usage is recorded

- **Install receipt.** Installing a skill records an `InstallReceipt`
  (`{skill_hash, manifest_hash, install_id, timestamp}`) into a dedicated
  lineage-spine run (`run_id="skills"`). The receipt bytes are the artifact
  the spine hashes, so the returned anchor is the spine entry hash over the
  receipt itself.
- **Usage link.** `record_usage()` appends a line to
  `.sdd/skills/usage/<skill_hash>.jsonl` binding the skill hash to a run's
  journal head (the spine head hash) — a pointer into the Merkle-chained run
  journal, not a copy of it — whenever a skill participates in a run.
- **Provenance graph.** `provenance` walks those usage links and returns
  only the runs whose journal head still verifies *and* still equals the
  head recorded at link time.

Receipt rows and usage rows are canonical JSON (sorted keys, minimal
separators), so two byte-identical inputs produce byte-identical files and
anchors.

## Limitation: usage linking is not yet wired into a run

The install receipt is wired end to end: `bernstein skills catalog install`
calls `write_install_receipt` on every install, so `bernstein skill verify`
has a real receipt to check against for any skill installed through the
catalog. As of this writing, however, no orchestration code path calls
`record_usage()` — the usage-link write side lives in
`core/skills/provenance.py` with no caller outside its own tests. Until a
run-time hook wires `record_usage()` into skill invocation,
`bernstein skill provenance` will correctly report "No recorded usage for
this skill" for every skill, regardless of how often it actually ran.

## Source

`src/bernstein/cli/commands/skill_cmd.py` (CLI),
`src/bernstein/core/skills/provenance.py` (install receipt, usage link,
provenance graph, verify). See [Skill catalog](skills-catalog.md) for the
install/lockfile surface this builds on.
