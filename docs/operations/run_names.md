# Memorable run names

Audience: operators triaging multiple parallel runs who need a short,
readable handle for a run instead of a seven-character UUID prefix.

## Overview

Run ids stay UUIDs internally. User-facing surfaces (CLI status output,
the run-archive summary) additionally show a deterministic memorable name
derived from the UUID, for example `swift-otter-07`. The UUID still
appears in detail views and machine-readable output, so nothing changes
for tooling that keys on the id.

The mapping is pure: the same UUID always renders to the same name. Logs,
dashboards, and status panels therefore stay consistent across restarts
and versions.

Source:

- `src/bernstein/cli/run_names.py` (rendering and reverse lookup helpers)
- `src/bernstein/cli/commands/run_names_cmd.py` (`run-lookup` command)

## Name format

A rendered name is `<adjective>-<noun>-<NN>`:

- `adjective` and `noun` come from two fixed, checked-in word lists.
- Words are English-only, lowercase, at most 8 characters, unambiguous,
  and non-product-specific.
- `NN` is a stable, zero-padded two-digit suffix (`00`-`99`).

The total name space is `len(ADJECTIVES) * len(NOUNS) * 100`.

## Rendering rule

`render_name(run_id)` derives the name as follows:

1. Compute `digest = blake2b(run_id.bytes, digest_size=8)` and read it as a
   big-endian unsigned 64-bit integer `h`. Hashing (rather than using the
   raw UUID integer) spreads adjacent or structured ids across the word
   space so they do not cluster onto neighbouring names.
2. `adjective = ADJECTIVES[h % len(ADJECTIVES)]`.
3. `noun = NOUNS[(h // len(ADJECTIVES)) % len(NOUNS)]`.
4. `suffix = (h // (len(ADJECTIVES) * len(NOUNS))) % 100`, rendered as two
   digits.

BLAKE2b is keyed on the raw 16 UUID bytes only, so the result does not
depend on the platform hash seed and stays identical across Python
versions and machines.

### Stability guarantee

Changing the word lists or the recipe would change every rendered name and
break stored references in logs and dashboards. Treat both as a stable
public contract; the test suite pins a golden value to catch accidental
drift.

## Collisions

Because 128 UUID bits cannot fit into the finite name space, the mapping is
not globally bijective: distinct UUIDs can render to the same name. The
collision probability is governed by the birthday bound and is low for the
handful of runs an operator triages at once.

When a reverse mapping is needed:

- `build_lookup(run_ids)` builds a name -> UUID map over a *known* set of
  ids, first-writer-wins.
- `find_collisions(run_ids)` reports any name shared by two or more of the
  supplied ids. Callers (for example a server populating a fleet view) can
  log this at startup as a configuration warning.

## Lookup command

`bernstein run-lookup NAME` resolves a display name within the known run
UUIDs: the active run ID (read from `.sdd/runtime/run_id`) and any IDs
passed with `--candidate`. A saved name alone does not identify a run
globally. The command can print multiple matching UUIDs:

```text
bernstein run-lookup swift-otter-07
bernstein run-lookup swift-otter-07 --candidate <uuid> --candidate <uuid>
bernstein run-lookup swift-otter-07 --workspace-root /path/to/project
```

Exit codes:

- `0` - one or more known ids render to NAME (printed one per line).
- `1` - NAME is well-formed but no known id renders to it.
- `2` - NAME is malformed (not `<adjective>-<noun>-NN`).

When more than one known id renders to NAME, the command prints a warning
before listing all matching UUIDs.

## Tested via

- `pytest tests/unit/cli/test_run_names.py`
