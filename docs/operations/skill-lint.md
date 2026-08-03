# Skill lint

`bernstein skills lint` runs an advisory check over an installed skill
directory. `bernstein skills install --strict` and `bernstein skills sync
--strict` run the same check and refuse the operation on any ERROR
finding; without `--strict`, findings are rendered but never block.

## Checks

| Code | Severity | What it catches |
|---|---|---|
| `missing-skill-md` / `unreadable-skill-md` | ERROR | No readable `SKILL.md` in the directory |
| `missing-frontmatter` / `yaml-error` / `frontmatter-shape` | ERROR | Frontmatter absent or not a YAML mapping |
| `invalid-manifest` | ERROR | Frontmatter fails `SkillManifest` validation |
| `extra-key` | WARNING | Unknown frontmatter key (e.g. Claude-style `whenToUse`) |
| `unsafe-reference-path` / `missing-reference` | ERROR | A `references`/`scripts`/`assets` entry escapes its bucket or is missing |
| `sensitive-pattern` | ERROR | Invisible Unicode codepoints the sanitiser strips — a likely hidden-instruction payload |
| `prompt-space-risk` | ERROR | Body text that instructs the agent beyond the skill's stated purpose (see below) |
| `body-too-large` / `missing-h1` | WARNING | Body over 5 KB, or body not starting with an H1 |

## Prompt-space risk findings

A skill body is prompt-space code: it steers the agent that loads it. The
lint scans the body for three instruction shapes and reports each as an
ERROR with code `prompt-space-risk`:

- **Exfiltration-shaped instructions** — an egress verb (`curl`, `upload`,
  `POST`, `send`, ...) next to a sensitive noun (`.env`, credentials,
  secrets, API keys, ...) on the same line.
- **Credential-file asks** — a read verb next to a credential artifact
  (`~/.aws/credentials`, `id_rsa`, `.ssh/`, `.netrc`, keychain), or a
  content-access verb (read, cat, print, dump, extract) next to `.env`.
  File-management guidance ("include `.env` in `.gitignore`", "copy
  `.env.example` to `.env`") stays clean.
- **Approval-bypass phrasing** — "ignore previous instructions", "skip the
  confirmation", "without asking", "do not tell the user", and similar.

Each check is conjunctive, so ordinary skill vocabulary stays clean:
"use environment variables for secrets" has no egress verb, "POST each
task to the task server" names nothing sensitive, and negated safeguards
("never merge without explicit approval", "never upload secrets") are
recognised as safeguards — the negation only counts inside its own
clause, so an unrelated "not" earlier in the sentence does not mask a
real instruction. Soft-wrapped paragraphs are scanned as one logical
line, so a line break in the middle of a phrase does not evade the
checks. The in-tree skill packs under `templates/skills/` lint clean by
regression test.

Findings name the shape and quote the first offending line:

```
prompt-space-risk: exfiltration-shaped instruction in body (body line 12): 'upload the contents of the .env file ...'
```

A refused body under `--strict` is a signal to review the skill's source,
not to reword around the pattern: if the flagged line is genuinely benign,
rewrite it so intent is unambiguous (the negated-safeguard form above is
the usual fix).
