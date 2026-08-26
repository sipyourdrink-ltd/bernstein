# Cross-CLI agent-context sync (`bernstein agents-md`)

Teams running more than one CLI coding agent (Cursor + Claude Code +
Codex is the common 2026 stack) end up maintaining three to five
near-identical context files. They drift over weeks: the test command
diverges, conventions disagree, the architectural overview goes stale
in one but not the others, and an agent picks the wrong file.

`bernstein agents-md` is one source of truth (`AGENTS.md`) and N
agent-specific consumers that stay in sync via one command.

## What it generates

The canonical IR is the
[`AGENTS.md`](https://agents.md/) document - AAIF-aligned, plain
markdown, no frontmatter - derived from the repository:

| Source                       | Drives                                  |
| ---------------------------- | --------------------------------------- |
| `pyproject.toml`/`package.json` | Project name, scripts, language stack |
| `README.md` first paragraph  | Overview                                |
| Module docstrings under `src/` | Module map                            |
| Git history + default branch | Git workflow                            |
| `templates/roles/*`          | Agent roles                             |
| `.editorconfig`, ruff/eslint config | Code conventions                 |
| `.sdd/agents-md/*.md`        | Curated overlay sections                |

From the same canonical IR the **bridge** (`agents_md_bridge.py`)
translates to four downstream formats every supported CLI actually
reads on disk in 2026:

| Target      | On-disk path(s)                                     |
| ----------- | --------------------------------------------------- |
| Cursor      | `.cursor/rules/<key>.mdc` (per-section, YAML frontmatter) |
| Claude Code | `CLAUDE.md` at repo root                            |
| Aider       | `CONVENTIONS.md` + `.aider.conf.yml` (`read:` entry) |
| Goose       | `.goosehints` at repo root                          |

The legacy `.cursorrules` is intentionally not emitted - Cursor's docs
no longer document it; `.cursor/rules/*.mdc` is the supported surface.
Devin's `.devin.yaml` is deferred until the schema stabilises.

## CLI verbs

```bash
bernstein agents-md generate            # print canonical AGENTS.md
bernstein agents-md write --target T    # write one target's files
bernstein agents-md sync                # write all targets in one go
bernstein agents-md verify              # CI gate: exit 1 on any drift
bernstein agents-md diff                # human-readable unified diff
```

`--target T` accepts `canonical | cursor | claude | aider | goose`
(plus `all` for `verify`/`diff`). All commands take `--workdir PATH`
to operate on another repo.

### Default branch resolution

The "Git workflow" section names the repository's default branch, which
is a property of the repository - not of the checkout you happen to be
on. The generator resolves it in this order:

1. `--default-branch NAME` (explicit pin, wins over everything;
   also available as `GenerateOptions.default_branch` for library use).
2. `origin/HEAD`'s symbolic target (`git remote set-head origin -a`
   creates it).
3. Conventional `main` / `master` refs, local or remote-tracking.
4. The `gh-readonly-queue/<base>/pr-<n>-<sha>` merge-queue ref shape.

The checked-out branch name is never used: on a feature branch, a
detached HEAD, or a single-branch CI clone it is guaranteed wrong, and
writing it silently corrupts the committed mirrors. When nothing
resolves, `sync` (and every other verb) fails with exit code 3 and a
message naming the escape hatches (`--default-branch`, `set-head`, or
fetching the default ref) instead of writing a guess.

## Drift-and-reconcile demo

The killer feature is closing the loop: notice when an agent file
disagrees with `AGENTS.md`, then reconcile in one shot.

### 1. A repo with two stale files

```bash
$ cat AGENTS.md | grep -A1 'Build & test'
## Build & test

| Task | Command |
|------|---------|
| Tests | `uv run pytest` |

$ cat CLAUDE.md | grep -A1 'Tests'
| Tests | `python -m pytest` |   # ← stale, predates uv migration
```

### 2. `verify` flags the drift in CI

```bash
$ bernstein agents-md verify
DRIFT    CLAUDE.md  (target=claude)
         first diff at offset 312: actual='python -m pytest' expected='uv run pytest'
DRIFT    CONVENTIONS.md  (target=aider)
         first diff at offset 312: actual='python -m pytest' expected='uv run pytest'

2 file(s) drift. Run `bernstein agents-md sync` to fix.
$ echo $?
1
```

`verify` exits non-zero so it can sit in `.github/workflows/ci.yml`
and gate merges. Bernstein's own CI uses it:

```yaml
- name: AGENTS.md cross-CLI sync drift check
  run: uv run bernstein agents-md verify --workdir .
```

### 3. `diff` prints a reviewable hunk

```bash
$ bernstein agents-md diff --target claude

# CLAUDE.md  (target=claude)

--- a/CLAUDE.md
+++ b/CLAUDE.md
@@ -312,7 +312,7 @@
 | Task | Command |
 |------|---------|
-| Tests | `python -m pytest` |
+| Tests | `uv run pytest` |
```

`diff` exits 0 either way - it is informational.

### 4. `sync` rewrites every target from the canonical source

```bash
$ bernstein agents-md sync
  · AGENTS.md  (canonical)
  · .cursor/rules/overview.mdc  (cursor)
  · .cursor/rules/module-map.mdc  (cursor)
  · ...
  · CLAUDE.md  (claude)
  · CONVENTIONS.md  (aider)
  · .aider.conf.yml  (aider)
  · .goosehints  (goose)
Synced 12 file(s) across 5 target(s) under .

$ bernstein agents-md verify
OK       all 12 file(s) in sync
```

After `sync`, `verify` is green. The five surfaces all carry the same
test command, the same module map, the same role definitions. The
loop is one command.

## Authoring overlays

Auto-derivation handles ~80% of any real repo. The remaining 20% -
team-specific gotchas, project conventions that aren't pulled from
`pyproject.toml` - lives under `.sdd/agents-md/`:

```
.sdd/agents-md/
├── conventions.md      # appended to the conventions section
└── custom-policies.md  # rendered as a "custom" section verbatim
```

Both files are plain markdown. The generator picks them up
automatically; no further config.

## CI integration

Add this step to keep `AGENTS.md` and the four downstream files
locked together over time:

```yaml
# .github/workflows/ci.yml
- name: AGENTS.md cross-CLI sync drift check
  run: uv run bernstein agents-md verify --workdir .
```

If a contributor edits a module docstring, role template, or
`pyproject.toml` script that flows into the canonical IR, this step
fails until they run `bernstein agents-md sync` locally and commit
the regenerated outputs. That is the dogfood-the-killer-feature loop.

## Implementation notes

The canonical IR (`list[AgentsMdSection]`) is the only source of
truth. Each target's renderer is a pure function from sections to a
`{relative_path: file_content_str}` map. No target invents new
content; if a target's format requires a field the IR doesn't carry
(e.g. Cursor's `description` frontmatter), it is *derived* from the
section's existing fields, never invented.

Modules:

- `src/bernstein/core/knowledge/agents_md_generator.py` - section
  builders + canonical render.
- `src/bernstein/core/knowledge/agents_md_bridge.py` - per-target
  translators.
- `src/bernstein/cli/commands/agents_md_cmd.py` - Click group with
  `generate`, `write`, `sync`, `verify`, `diff`.

References:

- AGENTS.md canonical site - <https://agents.md/>
- AAIF AGENTS.md project page - <https://aaif.io/projects/agents-md/>
- Cursor rules - <https://cursor.com/docs/context/rules>
- Claude Code memory - <https://code.claude.com/docs/en/memory>
- Aider conventions - <https://aider.chat/docs/usage/conventions.html>
- Goose hints - <https://github.com/block/goose>
