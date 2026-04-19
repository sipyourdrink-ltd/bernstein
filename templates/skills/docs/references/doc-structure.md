# Doc information architecture

Adapted from Diátaxis. A complete knowledge base has all four of:

- **Tutorials** — learning by doing, beginner-oriented.
- **How-to guides** — task-oriented, assume competence.
- **Reference** — dry, exhaustive, machine-friendly.
- **Explanation** — understanding-oriented, describes "why".

## Structure
```
docs/
  tutorials/        # beginner-first, single focused journey
  how-to/           # cookbook entries
  reference/        # API, CLI, config schemas
  architecture/     # ADRs, explanations, decision logs
  faq.md            # short, discoverable answers
```

## Hygiene
- A README links to every top-level directory.
- Every public API has reference-level coverage.
- Tutorials stay short; long ones are split into chapters.
- Explanations are dated — architecture evolves.
