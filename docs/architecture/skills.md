# Skills — progressive-disclosure capability packs

Status: active (oai-004, April 2026).
Applies to: all CLI adapters spawned by Bernstein.

## Motivation

Before oai-004 every agent spawn loaded the full ``templates/roles/<role>/system_prompt.md``
into the system prompt, whether or not the agent actually exercised the
deep guidance. Across 17 roles the bodies averaged ~40 lines each; the
token bill was paid on every spawn, including retries and forks.

OpenAI's Agents SDK v2 published the **Skills** pattern: a capability
pack is a directory with ``SKILL.md`` (YAML frontmatter + markdown body)
and optional ``references/``, ``scripts/``, ``assets/`` siblings.
Callers receive only an index (``name + description`` per skill) and
pull the full body on demand via ``load_skill``.

Bernstein adopts the same shape. Roles are migrated to skill packs and
the resolver injects just the index into the system prompt. Agents
``load_skill`` when they decide a capability is relevant.

## Directory layout

```
templates/
  roles/                # legacy bodies (kept for backwards compat)
    backend/
      system_prompt.md
      task_prompt.md
      config.yaml
    …
  skills/               # new skill packs (oai-004)
    backend/
      SKILL.md
      references/
        python-conventions.md
        test-patterns.md
        error-handling.md
      scripts/
        lint.sh
    qa/
      SKILL.md
      references/
        test-strategy.md
        edge-cases.md
    …
```

Empty buckets (``references/``, ``scripts/``, ``assets/``) are omitted —
the manifest's corresponding field is just an empty list.

## ``SKILL.md`` format

```markdown
---
name: backend
description: Python server code, APIs, async, strict typing.
trigger_keywords: [python, backend, async, pyright]
references:
  - python-conventions.md
  - test-patterns.md
  - error-handling.md
scripts:
  - lint.sh
---

# Backend Engineering Skill

You are a backend engineer…
```

Descriptions stay terse (one line) because the full index ships in every
spawn's system prompt. Every byte multiplies by the number of agents
Bernstein launches.

### Frontmatter schema

Defined by :class:`bernstein.core.skills.SkillManifest` (Pydantic,
``extra="forbid"`` so typos fail loudly):

| field                | type          | notes                                  |
| -------------------- | ------------- | -------------------------------------- |
| ``name``             | ``str``       | matches ``^[a-z][a-z0-9-]*$``          |
| ``description``      | ``str``       | 20-500 chars, shown in the index       |
| ``trigger_keywords`` | ``list[str]`` | optional keyword hints                 |
| ``references``       | ``list[str]`` | files under ``<skill>/references/``    |
| ``scripts``          | ``list[str]`` | files under ``<skill>/scripts/``       |
| ``assets``           | ``list[str]`` | files under ``<skill>/assets/``        |
| ``version``          | ``str``       | defaults to ``"1.0.0"``                |
| ``author``           | ``str\|None`` | optional                               |

Validation failures raise
:class:`bernstein.core.skills.SkillManifestError` with the originating
path baked into the message.

## Resolution flow

``bernstein.core.planning.role_resolver.resolve_role_prompt`` is called
once per spawn. It tries three things in order:

1. **Skill pack** — ``templates/skills/<role>/SKILL.md`` exists → inject
   the compact index **plus** the matched skill body.
2. **Legacy role template** — no skill pack, but
   ``templates/roles/<role>/system_prompt.md`` exists → render via the
   existing Jinja-style engine and inject that.
3. **Fallback stub** — neither path found → ``"You are a <role> specialist."``

The resolver is cached per ``(templates_dir, skills/ mtime)`` so dev
reloads pick up edits but production spawns do not re-parse 17 manifests
on every tick.

## ``load_skill`` MCP tool

Registered by :mod:`bernstein.mcp.server` under the name ``load_skill``:

```python
async def load_skill(
    name: str,
    reference: str | None = None,
    script: str | None = None,
) -> dict: ...
```

Returns JSON with:

- ``name`` — echoed back.
- ``body`` — ``SKILL.md`` body when ``reference`` and ``script`` are unset.
- ``available_references`` / ``available_scripts`` — always populated.
- ``reference_content`` — the requested reference's raw text (only when
  ``reference`` was passed).
- ``script_content`` — the requested script's raw text.
- ``error`` — populated when the skill / file could not be loaded.

Every invocation emits a ``skill_loaded`` WAL event (best-effort) with
``name``, ``reference``, ``script``, ``source``, ``duration_s``, and
``error`` fields.

## Sources

Skills are aggregated from multiple sources into a single
:class:`SkillLoader`. Name collisions abort startup with
:class:`DuplicateSkillError` — duplicate names across sources are never
silently shadowed.

### First-party

``bernstein/templates/skills/`` loaded by
:class:`bernstein.core.skills.sources.LocalDirSkillSource`.

### Plugin packs

Register a zero-arg factory under ``bernstein.skill_sources``:

```toml
[project.entry-points."bernstein.skill_sources"]
my-data-pack = "my_pack.skills:source"
```

Where ``my_pack/skills.py`` exposes:

```python
from pathlib import Path

from bernstein.core.skills import SkillSource
from bernstein.core.skills.sources import LocalDirSkillSource


def source() -> SkillSource:
    return LocalDirSkillSource(
        Path(__file__).parent / "skills",
        source_name="plugin:my-data-pack",
    )
```

:func:`bernstein.core.skills.sources.load_plugin_sources` enumerates the
group at loader construction time. Broken factories log a warning and
are skipped rather than aborting startup — a noisy third-party bug
should not take down the orchestrator.

## CLI

```
bernstein skills list                 # compact table of every skill
bernstein skills show backend         # print SKILL.md body
bernstein skills show backend --reference python-conventions.md
bernstein skills show backend --script lint.sh
```

## Observability

Every successful ``load_skill`` invocation emits a structured
``skill_loaded`` event. Hook it into the WAL
(``src/bernstein/core/persistence/wal.py``) or a Prometheus metric
(``skill_load_total{name=..., source=...}``,
``skill_load_duration_seconds{name=...}``) to see:

- Which skills get exercised vs. sit dead.
- Whether agents converge on a small core set.
- Which references are worth keeping close and which can be retired.

Dead skills (zero loads in 30 days) become candidates for deprecation.

## Migration status

All 17 roles migrated to skill packs as of oai-004:

| role              | references                                                  | notes                          |
| ----------------- | ----------------------------------------------------------- | ------------------------------ |
| ``backend``       | python-conventions, test-patterns, error-handling + lint.sh | full split                     |
| ``qa``            | test-strategy, edge-cases                                   | full split                     |
| ``security``      | owasp-top-10, auth-checklist, secrets-handling              | full split                     |
| ``frontend``      | a11y, state-management                                      | full split                     |
| ``devops``        | ci-patterns, docker-practices                               | full split                     |
| ``architect``     | adr-template, decomposition-principles                      | full split                     |
| ``docs``          | docstring-style, doc-structure + check-links.sh             | full split                     |
| ``retrieval``     | hybrid-search, chunking                                     | full split                     |
| ``ml-engineer``   | evaluation, reproducibility                                 | full split                     |
| ``reviewer``      | review-rubric, feedback-tone                                | full split                     |
| ``manager``       | task-api, planning-rules                                    | full split                     |
| ``vp``            | pivot-evaluation, cell-decomposition                        | full split                     |
| ``prompt-engineer`` | —                                                         | body small, no references      |
| ``visionary``     | —                                                           | body is the output schema      |
| ``analyst``       | —                                                           | body is the scoring rubric     |
| ``resolver``      | —                                                           | single-purpose skill           |
| ``ci-fixer``      | —                                                           | single-purpose skill           |

Legacy ``templates/roles/<role>/system_prompt.md`` files remain on disk
for backwards compat. A follow-up ticket will deprecate the legacy path
two minor versions after this change ships.
