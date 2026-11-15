# Wiki build

`bernstein wiki build` renders a deterministic Markdown wiki locally from
three inputs: the selected repository's AST symbol graph, its tracked-file
list, and its repository name. It does not read an `agents.md` instruction
document or IR. With no output option, the Markdown is written to stdout.
`--write` writes `WIKI.md` at the selected repository root. The renderer is
pure and makes no network requests.

## Why it exists

Repo wikis exist as a hosted SaaS with per-seat pricing in several
products. The same surface area is a derived view over code that already lives on
disk: the symbol graph, the tracked-file list, and the repository name.
There is no reason a project cannot render the wiki on its own laptop, on
every commit, for free.

The build is also useful as a CI artefact. Because the output is
deterministic for a given repo state, a workflow can render
`WIKI.md`, diff against the committed copy, and fail if they drift.

## How to use it

```bash
# Stream the rendered wiki to stdout
bernstein wiki build

# Write to WIKI.md at the repo root
bernstein wiki build --write

# Custom output path (implies --write); relative paths are under --repo
bernstein wiki build --repo /path/to/other/repo --output reports/wiki.md

# An absolute output path is used as given
bernstein wiki build --repo /path/to/other/repo --output /tmp/wiki.md
```

`--repo` selects the repository to scan; it does not change the shell's
working directory. Thus the relative `reports/wiki.md` above is written to
`/path/to/other/repo/reports/wiki.md`, not under the current directory.

The command passes the renderer:

- The AST symbol graph built from tracked Python files in the selected repo.
- The tracked-file list from `git ls-files`.
- The selected repo's directory name, used in the wiki title.

The renderer uses these inputs to produce three sections: **Top-level
structure** (from tracked paths), **Public API summary** (public top-level
Python functions and classes from the symbol graph, grouped by package), and
**Test layout** (tracked Python test files and their directories). It does
not read `agents.md`, instructions, or an agent-context IR. Non-Python files
can contribute to the top-level structure, but are not listed as bare files
or analyzed for symbols. No filesystem mutation happens unless `--write`
or `--output` is set.

## Configuration

| Flag | Default | Meaning |
|---|--:|---|
| `--repo PATH` | current working directory | Repo root to scan. |
| `--write` | off | Write to `<repo>/WIKI.md`. |
| `--output PATH` | unset | Custom destination; implies `--write`. Relative paths are resolved under the selected repo; absolute paths are used as given. |

## Limitations

- This is the smallest viable slice of the wiki feature. HTTP
  routes, MCP server exposure, and post-commit re-indexing are
  not yet wired in.
- The renderer is read-only. It does not touch git history or push
  anywhere.
- The symbol graph is derived from tracked Python files. Other languages
  are not analyzed for symbols or emitted as bare file entries; their paths
  may contribute to the top-level structure section.

## Related

- Source: `src/bernstein/cli/commands/wiki_cmd.py`
- Symbol graph: `src/bernstein/core/knowledge/ast_symbol_graph.py`
- Renderer: `src/bernstein/core/knowledge/wiki_renderer.py`
- [Cross-CLI agent-context sync (agents-md)](../agents-md.md)
