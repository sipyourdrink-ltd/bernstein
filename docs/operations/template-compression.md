# Role-template compression

`bernstein templates compress` shrinks the role prompt templates that are
prepended to every worker spawn, using an LLM rewrite that is mechanically
validated, backed up out of tree, and receipted on the audit chain.
`bernstein templates restore` reverses it byte-identically.

Compression is **never automatic** - it runs only through this explicit,
operator-gated command.

Source:

- `src/bernstein/core/tokens/template_compression.py` - compress/restore
- `src/bernstein/core/tokens/template_compress_validate.py` - validators
- `src/bernstein/cli/commands/templates_cmd.py` - `compress` / `restore`

## Why a token cut here compounds

A role template is fixed prose that ships on *every* spawn for that role.
Trimming it once reduces the input tokens of every subsequent worker of
that role, so the saving is per-spawn and recurring rather than one-off.

## `bernstein templates compress`

```bash
bernstein templates compress reviewer
bernstein templates compress --all
bernstein templates compress backend --model <model> --provider openrouter --yes
```

| Argument / flag | Default | Meaning |
|-----------------|---------|---------|
| `ROLE` | - | Compress one role's templates. Mutually exclusive with `--all`. |
| `--all` | off | Compress every role template. |
| `--workdir DIR` | `.` | Project root the role templates are resolved from. |
| `--model NAME` | `anthropic/claude-haiku-4-5` | Model used for the rewrite. |
| `--provider NAME` | `openrouter` | Adapter/provider for the rewrite (`openrouter`, `openai`, ...). |
| `--yes` | off | Skip the confirmation prompt. |

You must provide exactly one of `ROLE` or `--all`.

### The safety pipeline

Every rewrite passes through the same fixed pipeline before it lands:

1. **LLM rewrite** through the configured adapter.
2. **Mechanical validators** - the base compaction validators plus
   template-specific checks: fenced code blocks, headings, URLs, inline
   code, placeholders, and the completion-contract block must all survive.
   At most two targeted fix-only retries are allowed; otherwise the
   compression aborts and the template is left untouched.
3. **Out-of-tree backup** of the originals, keyed by content hash, with
   readback verification.
4. **Chained receipt** appended to the audit chain.

The command prints only the template token delta
(`role: template reduced <pre> -> <post> tokens`). Per-spawn dollar
savings come from the ledger on subsequent runs, via
`bernstein cost --by role`. Compression itself never reports a dollar
figure it cannot yet observe.

### Backups

Originals are stored under `$XDG_DATA_HOME/bernstein/template-backups`
(falling back to `~/.local/share/bernstein/template-backups`), out of tree
by design and keyed by content hash. The backup location is printed in the
confirmation prompt.

### The receipt

The audit-chain event is `template.compression.receipt` and carries
`{role, pre_sha256, post_sha256, pre_tokens, post_tokens, validators,
adapter, model}`, with the previous chain digest embedded so the receipt
is position-locked. A row is also appended to a `templates.lock` file
mapping the role's pre and post directory digests.

## Interaction with team drift

That `templates.lock` row is what lets
[`bernstein team drift`](team-manifests.md) classify a compressed template
as an **intentional** change rather than reporting spurious drift against a
pinned team manifest. A compression explained by a receipted lock entry is
labelled `receipted template compression (intentional, not drift)`.

## `bernstein templates restore`

```bash
bernstein templates restore reviewer
```

| Argument / flag | Default | Meaning |
|-----------------|---------|---------|
| `ROLE` | - | Role whose templates to restore. |
| `--workdir DIR` | `.` | Project root the role templates are resolved from. |

Restore reads the out-of-tree backups keyed by content hash, verifies
every hash on the way in and the role directory digest on the way out, and
records the reversal on the audit chain. The result is byte-identical to
the pre-compression originals.

## Related

- [Named team manifests](team-manifests.md)
- [Cost optimization](cost-optimization.md)
- [Tiered context compaction](../architecture/memory_tiers.md)
