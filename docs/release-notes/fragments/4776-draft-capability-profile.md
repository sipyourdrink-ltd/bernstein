## 4776 - Draft capability profile helper

The onboarder now auto-drafts adapter capability profiles from CLI probe evidence, eliminating manual profile specification for new agents.

Key changes:
- Added ``bernstein.adapters.draft.Draft`` class that auto-drafts ``InvocationSpec`` from probe evidence
- The drafting helper discovers ``--model`` and ``--prompt`` flags in the help text and records their byte ranges for traceability
- Missing required fields trigger refusable exceptions that name the exact field missing (e.g., "missing required field(s): --model")
- The ``draft_from_evidence`` function validates that all profile fields trace back to the evidence, not hardcoded defaults
- When a CLI's --help output contains ``--model <name>``, the resulting InvocationSpec records the exact byte range of the model flag in the captured help text
- Profiles built from evidence produce reconstructable argv tokens, with every flag confirmed by the original help text
- The helper integrates with the onboarder to automatically generate profiles for newly-tracked agents, reducing manual configuration

This change affects:
- Onboarder for new agents (profiles auto-generated from probe evidence)
- The draft capability profile helper itself (new functionality)
- The adapter capability profile test suite (new tests for drafting)

No user-facing functional changes except that onboarders can now specify ``--model`` and ``--prompt`` visibility via evidence, not manual flags.